from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from reddit_mod_from_discord.config import Settings, load_settings
from reddit_mod_from_discord.discord_ui.report_view import ReportView, build_report_embed
from reddit_mod_from_discord.models import ReportViewPayload, ReportedItem
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import DemoRedditService, RedditService
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
        if settings.demo_mode:
            self.reddit = DemoRedditService()
        else:
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

        if self.settings.demo_mode:
            await self._post_demo_example()
            logger.info("Demo mode active; polling disabled")
            return

        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _post_demo_example(self) -> None:
        channel = await self._resolve_mod_channel()
        if channel is None:
            return

        # Always post a fresh demo message on startup.
        demo_fullname = f"demo-{int(time.time())}"
        now = time.time()

        fetched = await self._fetch_demo_submission(self.settings.demo_post_url)

        def _get_str(key: str, default: str) -> str:
            value = fetched.get(key)
            return value if isinstance(value, str) and value else default

        def _get_opt_str(key: str) -> str | None:
            value = fetched.get(key)
            return value if isinstance(value, str) and value else None

        def _get_float(key: str, default: float) -> float:
            value = fetched.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            return default
        payload = ReportViewPayload(
            fullname=demo_fullname,
            kind="submission",
            subreddit=_get_str("subreddit", self.settings.reddit_subreddit or "demo"),
            author=_get_str("author", "demo_user"),
            permalink=_get_str("permalink", self.settings.demo_post_url),
            link_url=_get_opt_str("link_url"),
            media_url=_get_opt_str("media_url"),
            thumbnail_url=_get_opt_str("thumbnail_url"),
            title=_get_str("title", "Demo reported post"),
            snippet=_get_str("snippet", "This is demo mode. Buttons log actions only."),
            num_reports=1,
            created_utc=_get_float("created_utc", now),
            locked=False,
            reports_ignored=False,
            removed=False,
            approved=False,
            user_reports=["Demo report reason x1"],
            mod_reports=[],
        )

        if isinstance(self.reddit, DemoRedditService):
            self.reddit.seed(demo_fullname, user_reports=payload.user_reports, mod_reports=payload.mod_reports)

        dummy_report = ReportedItem(
            fullname=payload.fullname,
            kind=payload.kind,
            subreddit=payload.subreddit,
            author=payload.author,
            permalink=payload.permalink,
            link_url=payload.link_url,
            media_url=payload.media_url,
            thumbnail_url=payload.thumbnail_url,
            title=payload.title,
            snippet=payload.snippet,
            num_reports=payload.num_reports,
            created_utc=payload.created_utc,
            locked=payload.locked,
            reports_ignored=payload.reports_ignored,
            removed=payload.removed,
            approved=payload.approved,
            user_reports=payload.user_reports,
            mod_reports=payload.mod_reports,
        )
        # Record in DB for view persistence/history, but do not dedupe demo messages.
        try:
            await self.store._require_conn().execute(
                """
                INSERT OR REPLACE INTO reported_items (
                    fullname,
                    thing_kind,
                    subreddit,
                    first_reported_at,
                    last_seen_at,
                    report_count,
                    handled
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    dummy_report.fullname,
                    dummy_report.kind,
                    dummy_report.subreddit,
                    time.time(),
                    time.time(),
                    dummy_report.num_reports,
                ),
            )
            await self.store._require_conn().commit()
        except Exception:
            pass

        view = ReportView(
            payload=payload,
            store=self.store,
            reddit=self.reddit,
            allowed_role_ids=self.allowed_role_ids,
            demo_mode=True,
        )
        try:
            sent = await channel.send(
                embed=build_report_embed(payload),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=self.settings.discord_silent_notifications,
            )
        except discord.Forbidden:
            logger.error(
                "Cannot send to #%s (%s): Missing Access. Check channel permissions for the bot role.",
                getattr(channel, "name", "unknown"),
                channel.id,
            )
            return
        except discord.HTTPException:
            logger.exception("Failed to post demo message")
            return
        try:
            self.add_view(view, message_id=sent.id)
        except ValueError:
            pass
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

    async def _fetch_demo_submission(self, url: str) -> dict[str, object]:
        settings = self.settings
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            return {}

        import html
        import praw

        def fetch_sync() -> dict[str, object]:
            reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent,
            )
            submission = reddit.submission(url=url)
            _ = submission.title

            author_obj = getattr(submission, "author", None)
            author = getattr(author_obj, "name", "[deleted]") if author_obj else "[deleted]"
            permalink = f"https://www.reddit.com{submission.permalink}"
            link_url = str(getattr(submission, "url", "") or "").strip() or None
            snippet = submission.selftext or link_url or ""

            thumbnail_url = None
            raw_thumb = getattr(submission, "thumbnail", None)
            if isinstance(raw_thumb, str) and raw_thumb.startswith("http"):
                thumbnail_url = raw_thumb

            media_url = None
            preview = getattr(submission, "preview", None)
            if isinstance(preview, dict):
                images = preview.get("images")
                if isinstance(images, list) and images:
                    source = images[0].get("source") if isinstance(images[0], dict) else None
                    if isinstance(source, dict):
                        u = source.get("url")
                        if isinstance(u, str) and u:
                            media_url = html.unescape(u)
            if media_url is None and link_url:
                lowered = link_url.lower()
                if "i.redd.it/" in lowered or lowered.endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp")
                ):
                    media_url = link_url

            return {
                "title": str(getattr(submission, "title", "") or ""),
                "snippet": str(snippet),
                "author": author,
                "subreddit": str(getattr(submission.subreddit, "display_name", "") or ""),
                "created_utc": float(getattr(submission, "created_utc", time.time())),
                "permalink": permalink,
                "link_url": link_url,
                "media_url": media_url,
                "thumbnail_url": thumbnail_url,
            }

        try:
            return await asyncio.to_thread(fetch_sync)
        except Exception:
            return {}

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
        except discord.Forbidden:
            logger.error(
                "Missing access to channel_id=%s. Ensure the bot is in the server and has View Channel + Send Messages in that channel.",
                self.settings.discord_mod_channel_id,
            )
            return None
        except (discord.NotFound, discord.HTTPException):
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
            seen_fullnames: set[str] = set()
            for report in reports:
                seen_fullnames.add(report.fullname)
                payload = ReportViewPayload.from_reported_item(report)
                if not self._passes_threshold(payload):
                    continue
                should_alert = await self.store.should_alert(report)
                if not should_alert:
                    # Update existing alert message in-place if present.
                    await self._update_existing_alert(report)
                    continue

                view = ReportView(
                    payload=payload,
                    store=self.store,
                    reddit=self.reddit,
                    allowed_role_ids=self.allowed_role_ids,
                    demo_mode=self.settings.demo_mode,
                )

                try:
                    sent = await channel.send(
                        embed=build_report_embed(payload),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                        silent=self.settings.discord_silent_notifications,
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

            # Also refresh known unhandled alerts to catch external changes.
            await self._refresh_unhandled_alerts(skip_fullnames=seen_fullnames)
            return posted

    async def _update_existing_alert(self, report: ReportedItem) -> None:
        channel_id, message_id, handled = await self.store.get_alert_message(report.fullname)
        if handled:
            return
        if channel_id is None or message_id is None:
            return
        await self._edit_alert_message(
            fullname=report.fullname,
            channel_id=channel_id,
            message_id=message_id,
            new_report=report,
        )

    async def _refresh_unhandled_alerts(self, *, skip_fullnames: set[str]) -> None:
        try:
            refs = await self.store.list_unhandled_alerts(limit=50)
        except Exception:
            logger.exception("Failed to list unhandled alerts")
            return

        for fullname, channel_id, message_id in refs:
            if fullname in skip_fullnames:
                continue
            try:
                state = await self.reddit.refresh_state(fullname)
            except ValueError:
                # Most commonly: stale demo/test rows in the DB (e.g. t3_demo_...) or corrupted entries.
                # Mark handled to avoid repeatedly re-processing an invalid identifier.
                logger.warning("Invalid Reddit fullname in DB; marking handled: %s", fullname)
                try:
                    await self.store.mark_handled(fullname)
                except Exception:
                    pass
                continue
            except Exception:
                continue
            try:
                await self._edit_alert_message(
                    fullname=fullname,
                    channel_id=channel_id,
                    message_id=message_id,
                    refreshed_state=state,
                )
            except Exception:
                continue

    async def _edit_alert_message(
        self,
        *,
        fullname: str,
        channel_id: int,
        message_id: int,
        new_report: ReportedItem | None = None,
        refreshed_state: dict[str, object] | None = None,
    ) -> None:
        view_record = await self.store.get_view(message_id)
        if view_record is None:
            payload = ReportViewPayload.from_reported_item(new_report) if new_report else None
            if payload is None:
                return
        else:
            payload = ReportViewPayload.from_dict(view_record.payload)

        changed = False
        if new_report is not None:
            if payload.title != new_report.title:
                payload.title = new_report.title
                changed = True
            if payload.snippet != new_report.snippet:
                payload.snippet = new_report.snippet
                changed = True
            if payload.permalink != new_report.permalink:
                payload.permalink = new_report.permalink
                changed = True
            if payload.author != new_report.author:
                payload.author = new_report.author
                changed = True
            if payload.subreddit != new_report.subreddit:
                payload.subreddit = new_report.subreddit
                changed = True
            if payload.link_url != new_report.link_url:
                payload.link_url = new_report.link_url
                changed = True
            if payload.media_url != new_report.media_url:
                payload.media_url = new_report.media_url
                changed = True
            if payload.thumbnail_url != new_report.thumbnail_url:
                payload.thumbnail_url = new_report.thumbnail_url
                changed = True
            if payload.num_reports != new_report.num_reports:
                payload.num_reports = new_report.num_reports
                changed = True
            if payload.locked != new_report.locked:
                payload.locked = new_report.locked
                changed = True
            if payload.reports_ignored != new_report.reports_ignored:
                payload.reports_ignored = new_report.reports_ignored
                changed = True
            if payload.removed != new_report.removed:
                payload.removed = new_report.removed
                changed = True
            if payload.approved != new_report.approved:
                payload.approved = new_report.approved
                changed = True
            if payload.user_reports != new_report.user_reports:
                payload.user_reports = list(new_report.user_reports)
                changed = True
            if payload.mod_reports != new_report.mod_reports:
                payload.mod_reports = list(new_report.mod_reports)
                changed = True

        if refreshed_state is not None:
            raw = refreshed_state.get("locked")
            if isinstance(raw, bool) and payload.locked != raw:
                payload.locked = raw
                changed = True
            raw = refreshed_state.get("reports_ignored")
            if isinstance(raw, bool) and payload.reports_ignored != raw:
                payload.reports_ignored = raw
                changed = True
            raw = refreshed_state.get("removed")
            if isinstance(raw, bool) and payload.removed != raw:
                payload.removed = raw
                changed = True
            raw = refreshed_state.get("approved")
            if isinstance(raw, bool) and payload.approved != raw:
                payload.approved = raw
                changed = True
            raw = refreshed_state.get("num_reports")
            if isinstance(raw, (int, float)) and payload.num_reports != int(raw):
                payload.num_reports = int(raw)
                changed = True

        if not changed:
            return

        channel = self.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                fetched = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await self.store.clear_discord_message(fullname)
                await self.store.delete_view(message_id)
            except Exception:
                pass
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            try:
                await self.store.clear_discord_message(fullname)
                await self.store.delete_view(message_id)
            except Exception:
                pass
            return
        except (discord.Forbidden, discord.HTTPException):
            return

        view = ReportView(
            payload=payload,
            store=self.store,
            reddit=self.reddit,
            allowed_role_ids=self.allowed_role_ids,
            demo_mode=self.settings.demo_mode,
        )
        await message.edit(embed=build_report_embed(payload), view=view)
        await self.store.save_view(
            ViewRecord(
                message_id=message_id,
                channel_id=channel_id,
                guild_id=message.guild.id if message.guild else 0,
                payload=payload.to_dict(),
                created_at=time.time(),
            )
        )

    async def _restore_views(self) -> None:
        await self.store.prune_views(ttl_s=self.settings.view_store_ttl_hours * 3600)
        records = await self.store.load_views()
        restored = 0
        deleted = 0
        skipped = 0
        for record in records:
            try:
                payload = ReportViewPayload.from_dict(record.payload)
            except Exception:
                await self.store.delete_view(record.message_id)
                deleted += 1
                continue

            if not payload.fullname:
                await self.store.delete_view(record.message_id)
                deleted += 1
                continue

            view = ReportView(
                payload=payload,
                store=self.store,
                reddit=self.reddit,
                allowed_role_ids=self.allowed_role_ids,
                demo_mode=self.settings.demo_mode,
            )
            try:
                self.add_view(view, message_id=record.message_id)
            except ValueError:
                skipped += 1
                continue
            restored += 1

        if restored:
            logger.info("Restored %s persistent alert view(s)", restored)
        if deleted:
            logger.info("Deleted %s invalid alert view record(s)", deleted)
        if skipped:
            logger.debug("Skipped %s already-registered persistent view(s)", skipped)


def main() -> None:
    load_dotenv()
    settings = load_settings()
    logging.basicConfig(level=logging.DEBUG if settings.debug_logs else logging.INFO)
    bot = RedditModBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
