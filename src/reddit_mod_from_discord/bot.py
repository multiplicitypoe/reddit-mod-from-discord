from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from reddit_mod_from_discord.config import Settings, load_settings
from reddit_mod_from_discord.discord_ui.report_view import ReportView, build_report_embed
from reddit_mod_from_discord.models import ReportViewPayload
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import RedditService
from reddit_mod_from_discord.store import BotStore, ViewRecord

logger = logging.getLogger("reddit_mod_from_discord")


class RedditModBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = BotStore(settings.db_path)
        self.reddit = RedditService(settings)
        self.allowed_role_ids = set(settings.discord_allowed_role_ids)
        self._startup_done = False
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_lock = asyncio.Lock()

    async def on_ready(self) -> None:
        if self._startup_done:
            logger.info("Reconnected as %s", self.user)
            return
        self._startup_done = True

        logger.info("Logged in as %s", self.user)
        logger.info(
            "Config subreddit=r/%s poll_interval=%sm channel_id=%s",
            self.settings.reddit_subreddit,
            self.settings.poll_interval_minutes,
            self.settings.discord_mod_channel_id,
        )

        await self.store.connect()
        await self._register_commands()
        await self._restore_views()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self.store.close()
        await super().close()

    async def _register_commands(self) -> None:
        self.tree.clear_commands(guild=None)
        self.tree.add_command(
            app_commands.Command(
                name="modsync",
                description="Poll Reddit reports now.",
                callback=self._modsync_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="modhealth",
                description="Show bot health state.",
                callback=self._modhealth_command,
            )
        )
        synced = await self.tree.sync()
        logger.info("Synced %s command(s)", len(synced))

    async def _modsync_command(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_allowed_moderator(member, self.allowed_role_ids):
            await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await self._poll_once()
        await interaction.followup.send(f"Sync complete. Posted {posted} new alert(s).", ephemeral=True)

    async def _modhealth_command(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_allowed_moderator(member, self.allowed_role_ids):
            await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = await self._resolve_mod_channel()
        channel_ok = channel is not None
        await interaction.followup.send(
            "\n".join(
                [
                    f"Subreddit: r/{self.settings.reddit_subreddit}",
                    f"Poll interval: {self.settings.poll_interval_minutes} minute(s)",
                    f"Role allowlist size: {len(self.allowed_role_ids)}",
                    f"Mod channel resolved: {channel_ok}",
                ]
            ),
            ephemeral=True,
        )

    async def _resolve_mod_channel(self) -> discord.TextChannel | None:
        channel = self.get_channel(self.settings.discord_mod_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.fetch_channel(self.settings.discord_mod_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    def _passes_threshold(self, payload: ReportViewPayload) -> bool:
        if payload.kind == "comment":
            return payload.num_reports >= self.settings.comment_report_threshold
        return payload.num_reports >= self.settings.post_report_threshold

    async def _poll_loop(self) -> None:
        await asyncio.sleep(2)
        while not self.is_closed():
            try:
                posted = await self._poll_once()
                if posted:
                    logger.info("Poll posted %s new alert(s)", posted)
            except Exception:
                logger.exception("Poll cycle failed")
            await asyncio.sleep(self.settings.poll_interval_minutes * 60)

    async def _poll_once(self) -> int:
        async with self._poll_lock:
            channel = await self._resolve_mod_channel()
            if channel is None:
                logger.warning("Mod channel %s not found", self.settings.discord_mod_channel_id)
                return 0

            try:
                reports = await self.reddit.fetch_reports()
            except Exception:
                logger.exception("Failed to fetch Reddit reports")
                return 0

            posted = 0
            for report in reports:
                payload = ReportViewPayload.from_reported_item(report)
                if not self._passes_threshold(payload):
                    continue
                should_alert = await self.store.should_alert(report)
                if not should_alert:
                    continue

                view = ReportView(
                    payload=payload,
                    store=self.store,
                    reddit=self.reddit,
                    allowed_role_ids=self.allowed_role_ids,
                )

                try:
                    sent = await channel.send(
                        embed=build_report_embed(payload),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                        silent=True,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to post alert for %s", payload.fullname)
                    continue

                await self.store.set_discord_message(payload.fullname, sent.channel.id, sent.id)
                await self.store.save_view(
                    ViewRecord(
                        message_id=sent.id,
                        channel_id=sent.channel.id,
                        guild_id=sent.guild.id if sent.guild else 0,
                        payload=payload.to_dict(),
                        created_at=time.time(),
                    )
                )
                posted += 1
            return posted

    async def _restore_views(self) -> None:
        await self.store.prune_views(ttl_s=self.settings.view_store_ttl_hours * 3600)
        records = await self.store.load_views()
        restored = 0
        for record in records:
            channel = self.get_channel(record.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    fetched = await self.fetch_channel(record.channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await self.store.delete_view(record.message_id)
                continue

            try:
                message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.store.delete_view(record.message_id)
                continue

            try:
                payload = ReportViewPayload.from_dict(record.payload)
            except Exception:
                await self.store.delete_view(record.message_id)
                continue

            if not payload.fullname:
                await self.store.delete_view(record.message_id)
                continue

            view = ReportView(
                payload=payload,
                store=self.store,
                reddit=self.reddit,
                allowed_role_ids=self.allowed_role_ids,
            )
            try:
                self.add_view(view, message_id=record.message_id)
            except ValueError:
                await self.store.delete_view(record.message_id)
                continue

            # Best-effort: update components to match current layout.
            try:
                await message.edit(view=view)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            restored += 1

        if restored:
            logger.info("Restored %s persistent alert view(s)", restored)


def main() -> None:
    load_dotenv()
    settings = load_settings()
    logging.basicConfig(level=logging.DEBUG if settings.debug_logs else logging.INFO)
    bot = RedditModBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
