from __future__ import annotations

import asyncio
import os
import time

import discord
from dotenv import load_dotenv

from reddit_mod_from_discord.config import load_settings
from reddit_mod_from_discord.discord_ui.report_view import ReportView, build_report_embed
from reddit_mod_from_discord.models import ReportViewPayload, ReportedItem
from reddit_mod_from_discord.reddit_client import RedditService
from reddit_mod_from_discord.store import BotStore, ViewRecord


def _env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    return float(raw)


def _build_reported_submission_from_url_sync(settings, url: str) -> ReportedItem:
    import praw
    import html
    from urllib.parse import urlparse

    if settings.reddit_refresh_token:
        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            refresh_token=settings.reddit_refresh_token,
            user_agent=settings.reddit_user_agent,
        )
    else:
        if not settings.reddit_username or not settings.reddit_password:
            raise RuntimeError("Missing Reddit credentials")
        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            username=settings.reddit_username,
            password=settings.reddit_password,
            user_agent=settings.reddit_user_agent,
        )

    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    is_submission_url = "comments" in path_parts

    if is_submission_url:
        submission = reddit.submission(url=url)
        _ = submission.title
    else:
        subreddit_name = settings.reddit_subreddit
        if len(path_parts) >= 2 and path_parts[0].lower() == "r":
            subreddit_name = path_parts[1]
        subreddit = reddit.subreddit(subreddit_name)
        try:
            submission = next(subreddit.new(limit=1))
        except StopIteration:
            raise RuntimeError(f"No posts found in r/{subreddit_name}")
    author_obj = getattr(submission, "author", None)
    author = getattr(author_obj, "name", "[deleted]") if author_obj else "[deleted]"
    permalink = f"https://www.reddit.com{submission.permalink}"
    link_url = str(getattr(submission, "url", "") or "").strip() or None
    snippet = submission.selftext or link_url or ""
    fullname = str(getattr(submission, "name", "")) or f"t3_{submission.id}"
    num_reports = int(getattr(submission, "num_reports", 1) or 1)

    def fmt_reports(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                reason, count = entry[0], entry[1]
                out.append(f"{str(reason)} x{count}")
            else:
                out.append(str(entry))
        return out

    user_reports = fmt_reports(getattr(submission, "user_reports", []))
    mod_reports = fmt_reports(getattr(submission, "mod_reports", []))

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
                url = source.get("url")
                if isinstance(url, str) and url:
                    media_url = html.unescape(url)
    if media_url is None and link_url:
        lowered = link_url.lower()
        if "i.redd.it/" in lowered or lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            media_url = link_url

    return ReportedItem(
        fullname=fullname,
        kind="submission",
        subreddit=str(getattr(submission.subreddit, "display_name", settings.reddit_subreddit)),
        author=author,
        permalink=permalink,
        link_url=link_url,
        media_url=media_url,
        thumbnail_url=thumbnail_url,
        title=str(getattr(submission, "title", "(no title)") or "(no title)"),
        snippet=str(snippet),
        num_reports=num_reports,
        created_utc=float(getattr(submission, "created_utc", time.time())),
        locked=bool(getattr(submission, "locked", False)),
        reports_ignored=bool(getattr(submission, "ignore_reports", False)),
        removed=bool(
            getattr(submission, "removed_by_category", None)
            or getattr(submission, "banned_by", None)
        ),
        approved=bool(getattr(submission, "approved_by", None)),
        user_reports=user_reports,
        mod_reports=mod_reports,
    )


def _build_reported_comment_sync(settings) -> ReportedItem:
    import praw

    if settings.reddit_refresh_token:
        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            refresh_token=settings.reddit_refresh_token,
            user_agent=settings.reddit_user_agent,
        )
    else:
        if not settings.reddit_username or not settings.reddit_password:
            raise RuntimeError("Missing Reddit credentials")
        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            username=settings.reddit_username,
            password=settings.reddit_password,
            user_agent=settings.reddit_user_agent,
        )

    def fmt_reports(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                reason, count = entry[0], entry[1]
                out.append(f"{str(reason)} x{count}")
            else:
                out.append(str(entry))
        return out

    comment_url = os.getenv("TEST_REDDIT_COMMENT_URL")
    if comment_url:
        comment = reddit.comment(url=comment_url)
        _ = comment.body
    else:
        subreddit = reddit.subreddit(settings.reddit_subreddit)
        comment = next(subreddit.comments(limit=1))

    author_obj = getattr(comment, "author", None)
    author = getattr(author_obj, "name", "[deleted]") if author_obj else "[deleted]"
    permalink = f"https://www.reddit.com{comment.permalink}"
    fullname = str(getattr(comment, "name", "")) or f"t1_{comment.id}"
    num_reports = int(getattr(comment, "num_reports", 1) or 1)
    title = str(getattr(comment, "link_title", "Comment") or "Comment")
    snippet = str(getattr(comment, "body", "") or "")

    return ReportedItem(
        fullname=fullname,
        kind="comment",
        subreddit=str(getattr(comment.subreddit, "display_name", settings.reddit_subreddit)),
        author=author,
        permalink=permalink,
        link_url=None,
        media_url=None,
        thumbnail_url=None,
        title=title,
        snippet=snippet,
        num_reports=num_reports,
        created_utc=float(getattr(comment, "created_utc", time.time())),
        locked=bool(getattr(comment, "locked", False)),
        reports_ignored=bool(getattr(comment, "ignore_reports", False)),
        removed=bool(
            getattr(comment, "removed_by_category", None) or getattr(comment, "banned_by", None)
        ),
        approved=bool(getattr(comment, "approved_by", None)),
        user_reports=fmt_reports(getattr(comment, "user_reports", [])),
        mod_reports=fmt_reports(getattr(comment, "mod_reports", [])),
    )


class TestDiscordBot(discord.Client):
    def __init__(self, token: str, channel_id: int) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)
        self._token = token
        self._channel_id = channel_id

    async def on_ready(self) -> None:
        try:
            channel = self.get_channel(self._channel_id)
            if not isinstance(channel, discord.TextChannel):
                try:
                    fetched = await self.fetch_channel(self._channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                channel = fetched if isinstance(fetched, discord.TextChannel) else None

            if channel is None:
                print(f"Could not resolve DISCORD_MOD_CHANNEL_ID={self._channel_id}")
                return

            store_to_close: BotStore | None = None
            try:
                settings = load_settings()
                store_obj = BotStore(settings.db_path)
                store_to_close = store_obj
                await store_obj.connect()
                reddit = RedditService(settings)

                kind = (os.getenv("TEST_KIND") or "submission").strip().lower()
                if kind == "comment":
                    reported = await asyncio.to_thread(_build_reported_comment_sync, settings)
                else:
                    url = os.getenv("TEST_REDDIT_URL") or f"https://www.reddit.com/r/{settings.reddit_subreddit}/"
                    reported = await asyncio.to_thread(_build_reported_submission_from_url_sync, settings, url)
                payload = ReportViewPayload.from_reported_item(reported)
                payload.action_log.append("test: posted via make test-discord")

                view = ReportView(
                    payload=payload,
                    store=store_obj,
                    reddit=reddit,
                    allowed_role_ids=set(settings.discord_allowed_role_ids),
                )

                print(f"Sending full-feature test alert to #{channel.name} ({channel.id})")
                print(f"Kind: {payload.kind}")
                print(f"Using URL: {payload.permalink}")

                sent = await channel.send(
                    embed=build_report_embed(payload),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=True,
                )

                await store_obj.save_view(
                    ViewRecord(
                        message_id=sent.id,
                        channel_id=sent.channel.id,
                        guild_id=sent.guild.id if sent.guild else 0,
                        payload=payload.to_dict(),
                        created_at=time.time(),
                    )
                )

                stay_open_s = _env_float("TEST_DISCORD_STAY_OPEN_SECONDS", 0.0)
                if stay_open_s > 0:
                    print(
                        f"Keeping process alive for {stay_open_s:.0f}s so buttons can be tested..."
                    )
                    await asyncio.sleep(stay_open_s)

                print("Sent test alert.")
            except Exception as exc:
                print(f"Failed to send test alert: {exc}")
            finally:
                if store_to_close is not None:
                    await store_to_close.close()
        finally:
            await self.close()

    def run_bot(self) -> None:
        self.run(self._token)


def main() -> None:
    load_dotenv()
    token = _env_required("DISCORD_TOKEN")
    channel_id = _env_int("DISCORD_MOD_CHANNEL_ID", 604768963741876255)
    bot = TestDiscordBot(token=token, channel_id=channel_id)
    bot.run_bot()


if __name__ == "__main__":
    main()
