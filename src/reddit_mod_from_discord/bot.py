from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from dotenv import load_dotenv

from reddit_mod_from_discord.config import ResolvedSettings, Settings, load_settings, resolve_settings
from reddit_mod_from_discord.discord_ui.report_view import ReportView, build_report_embed
from reddit_mod_from_discord.logging_filters import install_discord_reconnect_log_compaction
from reddit_mod_from_discord.models import ReportViewPayload, ReportedItem
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import DemoRedditService, RedditService
from reddit_mod_from_discord.safety import sanitize_http_url
from reddit_mod_from_discord.store import BotStore, ViewRecord

logger = logging.getLogger("reddit_mod_from_discord")


@dataclass
class SetupRuntime:
    setup_id: str
    guild_id: int
    settings: ResolvedSettings
    reddit: RedditService | DemoRedditService
    allowed_role_ids: set[int]
    poll_task: asyncio.Task[None] | None
    poll_lock: asyncio.Lock


class RedditModBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = BotStore(settings.db_path)
        self._settings_cache: dict[str, ResolvedSettings] = {}
        self._runtimes: dict[str, SetupRuntime] = {}
        self._runtimes_by_guild: dict[int, list[str]] = {}
        self._startup_done = False

    async def on_ready(self) -> None:
        if self._startup_done:
            logger.info("Reconnected as %s", self.user)
            return
        self._startup_done = True

        logger.info("Logged in as %s", self.user)

        await self.store.connect()
        await self._register_commands()
        self._ensure_runtimes()
        await self._validate_guild_settings()
        await self._restore_views()

        if self.settings.demo_mode:
            for runtime in self._runtimes.values():
                guild = self.get_guild(runtime.guild_id)
                if guild is None:
                    continue
                await self._post_demo_example(guild, runtime)
            logger.info("Demo mode active; polling disabled")
            return

        for runtime in self._runtimes.values():
            guild = self.get_guild(runtime.guild_id)
            if guild is None:
                continue
            runtime.poll_task = asyncio.create_task(self._poll_loop(guild, runtime))

    async def _post_demo_example(self, guild: discord.Guild, runtime: SetupRuntime) -> None:
        channel = await self._resolve_mod_channel(guild, runtime.settings)
        if channel is None:
            return

        # Always post a fresh demo message on startup.
        demo_fullname = f"demo-{int(time.time())}"
        now = time.time()

        fetched = await self._fetch_demo_submission(runtime.settings, self.settings.demo_post_url)

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
            subreddit=_get_str("subreddit", runtime.settings.reddit_subreddit or "demo"),
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
            setup_id=runtime.setup_id,
        )

        if isinstance(runtime.reddit, DemoRedditService):
            runtime.reddit.seed(
                demo_fullname,
                user_reports=payload.user_reports,
                mod_reports=payload.mod_reports,
            )

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
                    setup_id,
                    guild_id,
                    fullname,
                    thing_kind,
                    subreddit,
                    first_reported_at,
                    last_seen_at,
                    report_count,
                    handled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    runtime.setup_id,
                    guild.id,
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
            reddit=runtime.reddit,
            allowed_role_ids=runtime.allowed_role_ids,
            demo_mode=True,
        )
        try:
            sent = await channel.send(
                embed=build_report_embed(payload),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=runtime.settings.discord_silent_notifications,
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
        await self.store.set_discord_message(
            payload.fullname,
            runtime.setup_id,
            sent.channel.id,
            sent.id,
        )
        await self.store.save_view(
            ViewRecord(
                message_id=sent.id,
                channel_id=sent.channel.id,
                guild_id=sent.guild.id if sent.guild else guild.id,
                payload=payload.to_dict(),
                created_at=time.time(),
            )
        )

    async def _fetch_demo_submission(
        self, settings: ResolvedSettings, url: str
    ) -> dict[str, object]:
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
            permalink = sanitize_http_url(f"https://www.reddit.com{submission.permalink}") or url
            link_url = sanitize_http_url(str(getattr(submission, "url", "") or ""))
            snippet = submission.selftext or link_url or ""

            thumbnail_url = None
            raw_thumb = getattr(submission, "thumbnail", None)
            if isinstance(raw_thumb, str):
                thumbnail_url = sanitize_http_url(raw_thumb)

            media_url = None
            preview = getattr(submission, "preview", None)
            if isinstance(preview, dict):
                images = preview.get("images")
                if isinstance(images, list) and images:
                    source = images[0].get("source") if isinstance(images[0], dict) else None
                    if isinstance(source, dict):
                        u = source.get("url")
                        if isinstance(u, str) and u:
                            media_url = sanitize_http_url(html.unescape(u))
            if media_url is None and link_url:
                lowered = link_url.lower()
                if "i.redd.it/" in lowered or lowered.endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp")
                ):
                    media_url = sanitize_http_url(link_url)

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
        for runtime in self._runtimes.values():
            if runtime.poll_task is None:
                continue
            runtime.poll_task.cancel()
            try:
                await runtime.poll_task
            except asyncio.CancelledError:
                pass
            runtime.poll_task = None
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
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        runtimes = self._get_runtimes_for_guild(interaction.guild.id)
        if not runtimes:
            await interaction.response.send_message(
                "No configuration found for this server.", ephemeral=True
            )
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        allowed = [runtime for runtime in runtimes if is_allowed_moderator(member, runtime.allowed_role_ids)]
        if not allowed:
            await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted_total = 0
        details: list[str] = []
        for runtime in allowed:
            posted = await self._poll_once(interaction.guild, runtime)
            posted_total += posted
            details.append(f"{runtime.setup_id}: {posted} alert(s)")
        detail_text = "\n".join(details)
        await interaction.followup.send(
            f"Sync complete. Posted {posted_total} new alert(s).\n{detail_text}",
            ephemeral=True,
        )

    async def _modhealth_command(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        runtimes = self._get_runtimes_for_guild(interaction.guild.id)
        if not runtimes:
            await interaction.response.send_message(
                "No configuration found for this server.", ephemeral=True
            )
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        allowed = [runtime for runtime in runtimes if is_allowed_moderator(member, runtime.allowed_role_ids)]
        if not allowed:
            await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines: list[str] = []
        for runtime in allowed:
            channel = await self._resolve_mod_channel(interaction.guild, runtime.settings)
            channel_ok = channel is not None
            lines.extend(
                [
                    f"Setup: {runtime.setup_id}",
                    f"Subreddit: r/{runtime.settings.reddit_subreddit}",
                    f"Poll interval: {runtime.settings.poll_interval_minutes} minute(s)",
                    f"Role allowlist size: {len(runtime.allowed_role_ids)}",
                    f"Mod channel resolved: {channel_ok}",
                    "",
                ]
            )
        if lines and not lines[-1]:
            lines.pop()
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def _resolve_mod_channel(
        self,
        guild: discord.Guild,
        settings: ResolvedSettings,
    ) -> discord.TextChannel | None:
        if settings.discord_mod_channel_id is None:
            return None
        channel = guild.get_channel(settings.discord_mod_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await guild.fetch_channel(settings.discord_mod_channel_id)
        except discord.Forbidden:
            logger.error(
                "Missing access to channel_id=%s. Ensure the bot is in the server and has View Channel + Send Messages in that channel.",
                settings.discord_mod_channel_id,
            )
            return None
        except (discord.NotFound, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    def _passes_threshold(self, payload: ReportViewPayload, settings: ResolvedSettings) -> bool:
        if payload.kind == "comment":
            return payload.num_reports >= settings.comment_report_threshold
        return payload.num_reports >= settings.post_report_threshold

    def _passes_age(self, payload: ReportViewPayload, settings: ResolvedSettings) -> bool:
        if settings.max_item_age_hours <= 0:
            return True
        if payload.created_utc <= 0:
            return True
        max_age_s = settings.max_item_age_hours * 3600
        return (time.time() - payload.created_utc) <= max_age_s

    async def _refresh_modlog_cache(self, runtime: SetupRuntime) -> None:
        if self.settings.demo_mode:
            return
        if runtime.settings.modlog_fetch_limit <= 0:
            return
        if not runtime.settings.reddit_subreddit:
            return
        try:
            last_seen = await self.store.get_modlog_state(runtime.setup_id)
            entries = await runtime.reddit.fetch_recent_modlog_entries(
                runtime.settings.reddit_subreddit,
                limit=runtime.settings.modlog_fetch_limit,
                min_created_utc=last_seen,
            )
            if entries:
                await self.store.save_modlog_entries(runtime.setup_id, entries)
                newest = max(entry[1] for entry in entries if entry[1])
                if newest:
                    await self.store.update_modlog_state(runtime.setup_id, newest)
            if runtime.settings.modlog_max_age_days > 0:
                await self.store.prune_modlog_entries(
                    runtime.setup_id,
                    runtime.settings.modlog_max_age_days * 86400,
                )
        except Exception:
            logger.exception("Failed to refresh modlog cache for %s", runtime.setup_id)

    async def _poll_loop(self, guild: discord.Guild, runtime: SetupRuntime) -> None:
        await asyncio.sleep(2)
        while not self.is_closed():
            try:
                posted = await self._poll_once(guild, runtime)
                if posted:
                    logger.info(
                        "Poll posted %s new alert(s) for guild %s",
                        posted,
                        guild.id,
                    )
            except Exception:
                logger.exception("Poll cycle failed for guild %s", guild.id)
            await asyncio.sleep(runtime.settings.poll_interval_minutes * 60)

    async def _poll_once(self, guild: discord.Guild, runtime: SetupRuntime) -> int:
        async with runtime.poll_lock:
            channel = await self._resolve_mod_channel(guild, runtime.settings)
            if channel is None:
                logger.warning(
                    "Mod channel %s not found for guild %s",
                    runtime.settings.discord_mod_channel_id,
                    guild.id,
                )
                return 0

            await self._refresh_modlog_cache(runtime)

            try:
                reports = await runtime.reddit.fetch_reports()
            except Exception:
                logger.exception("Failed to fetch Reddit reports for guild %s", guild.id)
                return 0

            posted = 0
            seen_fullnames: set[str] = set()
            for report in reports:
                seen_fullnames.add(report.fullname)
                payload = ReportViewPayload.from_reported_item(report, setup_id=runtime.setup_id)
                if not self._passes_age(payload, runtime.settings):
                    continue
                if not self._passes_threshold(payload, runtime.settings):
                    continue
                should_alert = await self.store.should_alert(report, runtime.setup_id, guild.id)
                if not should_alert:
                    # Update existing alert message in-place if present.
                    await self._update_existing_alert(guild, runtime, report)
                    continue

                if runtime.settings.modlog_fetch_limit > 0:
                    try:
                        max_age_s = (
                            runtime.settings.modlog_max_age_days * 86400
                            if runtime.settings.modlog_max_age_days > 0
                            else None
                        )
                        history = await self.store.list_modlog_entries(
                            runtime.setup_id,
                            payload.fullname,
                            max_age_s=max_age_s,
                            limit=runtime.settings.modlog_fetch_limit,
                        )
                        if history:
                            payload.action_log.extend(history)
                    except Exception:
                        logger.exception("Failed to load modlog cache for %s", payload.fullname)

                view = ReportView(
                    payload=payload,
                    store=self.store,
                    reddit=runtime.reddit,
                    allowed_role_ids=runtime.allowed_role_ids,
                    demo_mode=self.settings.demo_mode,
                )

                try:
                    sent = await channel.send(
                        embed=build_report_embed(payload),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                        silent=runtime.settings.discord_silent_notifications,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to post alert for %s", payload.fullname)
                    continue

                await self.store.set_discord_message(
                    payload.fullname,
                    runtime.setup_id,
                    sent.channel.id,
                    sent.id,
                )
                await self.store.save_view(
                    ViewRecord(
                        message_id=sent.id,
                        channel_id=sent.channel.id,
                        guild_id=sent.guild.id if sent.guild else guild.id,
                        payload=payload.to_dict(),
                        created_at=time.time(),
                    )
                )
                posted += 1

            # Also refresh known unhandled alerts to catch external changes.
            await self._refresh_unhandled_alerts(guild, runtime, skip_fullnames=seen_fullnames)
            return posted

    async def _update_existing_alert(
        self,
        guild: discord.Guild,
        runtime: SetupRuntime,
        report: ReportedItem,
    ) -> None:
        channel_id, message_id, handled = await self.store.get_alert_message(
            report.fullname,
            runtime.setup_id,
        )
        if handled:
            return
        if channel_id is None or message_id is None:
            return
        await self._edit_alert_message(
            guild,
            runtime,
            fullname=report.fullname,
            channel_id=channel_id,
            message_id=message_id,
            new_report=report,
        )

    async def _refresh_unhandled_alerts(
        self,
        guild: discord.Guild,
        runtime: SetupRuntime,
        *,
        skip_fullnames: set[str],
    ) -> None:
        try:
            refs = await self.store.list_unhandled_alerts(runtime.setup_id, limit=50)
        except Exception:
            logger.exception("Failed to list unhandled alerts")
            return

        for fullname, channel_id, message_id in refs:
            if fullname in skip_fullnames:
                continue
            try:
                state = await runtime.reddit.refresh_state(fullname)
            except ValueError:
                # Most commonly: stale demo/test rows in the DB (e.g. t3_demo_...) or corrupted entries.
                # Mark handled to avoid repeatedly re-processing an invalid identifier.
                logger.warning("Invalid Reddit fullname in DB; marking handled: %s", fullname)
                try:
                    await self.store.mark_handled(fullname, runtime.setup_id)
                except Exception:
                    pass
                continue
            except Exception:
                continue
            try:
                await self._edit_alert_message(
                    guild,
                    runtime,
                    fullname=fullname,
                    channel_id=channel_id,
                    message_id=message_id,
                    refreshed_state=state,
                )
            except Exception:
                continue

    async def _edit_alert_message(
        self,
        guild: discord.Guild,
        runtime: SetupRuntime,
        *,
        fullname: str,
        channel_id: int,
        message_id: int,
        new_report: ReportedItem | None = None,
        refreshed_state: dict[str, object] | None = None,
    ) -> None:
        view_record = await self.store.get_view(message_id)
        if view_record is None:
            payload = (
                ReportViewPayload.from_reported_item(new_report, setup_id=runtime.setup_id)
                if new_report
                else None
            )
            if payload is None:
                return
        else:
            payload = ReportViewPayload.from_dict(view_record.payload)
        if not payload.setup_id:
            payload.setup_id = runtime.setup_id

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
                payload.num_reports = max(0, int(raw))
                changed = True

        if runtime.settings.modlog_fetch_limit > 0:
            try:
                max_age_s = (
                    runtime.settings.modlog_max_age_days * 86400
                    if runtime.settings.modlog_max_age_days > 0
                    else None
                )
                history = await self.store.list_modlog_entries(
                    runtime.setup_id,
                    payload.fullname,
                    max_age_s=max_age_s,
                    limit=runtime.settings.modlog_fetch_limit,
                )
                if history:
                    existing = set(payload.action_log)
                    for line in history:
                        if line in existing:
                            continue
                        payload.action_log.append(line)
                        existing.add(line)
                        changed = True
            except Exception:
                logger.exception("Failed to load modlog cache for %s", payload.fullname)

        if not changed:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                fetched = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await self.store.clear_discord_message(fullname, runtime.setup_id)
                await self.store.delete_view(message_id)
            except Exception:
                pass
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            try:
                await self.store.clear_discord_message(fullname, runtime.setup_id)
                await self.store.delete_view(message_id)
            except Exception:
                pass
            return
        except (discord.Forbidden, discord.HTTPException):
            return

        view = ReportView(
            payload=payload,
            store=self.store,
            reddit=runtime.reddit,
            allowed_role_ids=runtime.allowed_role_ids,
            demo_mode=self.settings.demo_mode,
        )
        try:
            await message.edit(embed=build_report_embed(payload), view=view)
        except discord.NotFound:
            try:
                await self.store.clear_discord_message(fullname, runtime.setup_id)
                await self.store.delete_view(message_id)
            except Exception:
                pass
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to edit alert message %s", message_id)
            return
        await self.store.save_view(
            ViewRecord(
                message_id=message_id,
                channel_id=channel_id,
                guild_id=message.guild.id if message.guild else guild.id,
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

            setup_id = payload.setup_id or str(record.guild_id)
            runtime = self._runtimes.get(setup_id)
            if runtime is None:
                skipped += 1
                continue
            view = ReportView(
                payload=payload,
                store=self.store,
                reddit=runtime.reddit,
                allowed_role_ids=runtime.allowed_role_ids,
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

    def _get_resolved_settings(
        self, setup_id: str, overrides
    ) -> ResolvedSettings:
        cached = self._settings_cache.get(setup_id)
        if cached:
            return cached
        resolved = resolve_settings(self.settings, overrides)
        self._settings_cache[setup_id] = resolved
        return resolved

    def _build_runtime(
        self, setup_id: str, guild_id: int, overrides
    ) -> SetupRuntime:
        settings = self._get_resolved_settings(setup_id, overrides)
        reddit = DemoRedditService() if self.settings.demo_mode else RedditService(settings)
        allowed_role_ids = set(settings.discord_allowed_role_ids or ())
        return SetupRuntime(
            setup_id=setup_id,
            guild_id=guild_id,
            settings=settings,
            reddit=reddit,
            allowed_role_ids=allowed_role_ids,
            poll_task=None,
            poll_lock=asyncio.Lock(),
        )

    def _register_runtime(self, runtime: SetupRuntime) -> None:
        self._runtimes[runtime.setup_id] = runtime
        self._runtimes_by_guild.setdefault(runtime.guild_id, []).append(runtime.setup_id)

    def _ensure_runtimes(self) -> None:
        if self._runtimes:
            return
        if self.settings.multi_server_config:
            for setup in self.settings.multi_server_config.values():
                runtime = self._build_runtime(
                    setup.setup_id,
                    setup.guild_id,
                    setup.overrides,
                )
                self._register_runtime(runtime)
        else:
            for guild in self.guilds:
                setup_id = str(guild.id)
                runtime = self._build_runtime(setup_id, guild.id, None)
                self._register_runtime(runtime)

    def _get_runtimes_for_guild(self, guild_id: int) -> list[SetupRuntime]:
        setup_ids = self._runtimes_by_guild.get(guild_id, [])
        return [self._runtimes[setup_id] for setup_id in setup_ids if setup_id in self._runtimes]

    async def _validate_guild_settings(self) -> None:
        missing: list[str] = []
        for runtime in self._runtimes.values():
            guild = self.get_guild(runtime.guild_id)
            if guild is None:
                missing.append(f"{runtime.setup_id} (DISCORD_GUILD_ID {runtime.guild_id} not found)")
                continue
            resolved = runtime.settings
            missing_fields: list[str] = []
            if resolved.discord_mod_channel_id is None:
                missing_fields.append("DISCORD_MOD_CHANNEL_ID")
            if resolved.discord_allowed_role_ids is None:
                missing_fields.append("DISCORD_ALLOWED_ROLE_IDS")
            if not self.settings.demo_mode:
                if not resolved.reddit_client_id:
                    missing_fields.append("REDDIT_CLIENT_ID")
                if not resolved.reddit_client_secret:
                    missing_fields.append("REDDIT_CLIENT_SECRET")
                if not resolved.reddit_subreddit:
                    missing_fields.append("REDDIT_SUBREDDIT")
                if not resolved.reddit_refresh_token and not (
                    resolved.reddit_username and resolved.reddit_password
                ):
                    missing_fields.append(
                        "REDDIT_REFRESH_TOKEN or REDDIT_USERNAME+REDDIT_PASSWORD"
                    )
            if missing_fields:
                missing.append(f"{runtime.setup_id} ({', '.join(missing_fields)})")
                continue
            logger.info(
                "Config setup=%s guild=%s subreddit=r/%s poll_interval=%sm channel_id=%s",
                runtime.setup_id,
                runtime.guild_id,
                resolved.reddit_subreddit,
                resolved.poll_interval_minutes,
                resolved.discord_mod_channel_id,
            )
        if missing:
            missing_ids = ", ".join(missing)
            logger.error("Missing required settings for setup(s): %s", missing_ids)
            await self.close()
            raise RuntimeError("Missing required settings for setup(s): " + missing_ids)


def main() -> None:
    load_dotenv()
    settings = load_settings()
    logging.basicConfig(level=logging.DEBUG if settings.debug_logs else logging.INFO)
    install_discord_reconnect_log_compaction()
    bot = RedditModBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
