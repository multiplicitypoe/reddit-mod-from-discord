from __future__ import annotations

import asyncio
import html
import logging
import time

import praw
from praw.models import Comment, Submission

from dotenv import load_dotenv

from reddit_mod_from_discord.config import Settings, load_settings
from reddit_mod_from_discord.models import ReportedItem

logger = logging.getLogger("reddit_mod_from_discord")


def _squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


class RedditService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.reddit_refresh_token:
            self._reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                refresh_token=settings.reddit_refresh_token,
                user_agent=settings.reddit_user_agent,
            )
        else:
            if not settings.reddit_username or not settings.reddit_password:
                raise ValueError(
                    "Missing Reddit auth: set REDDIT_REFRESH_TOKEN or REDDIT_USERNAME/REDDIT_PASSWORD"
                )
            self._reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                username=settings.reddit_username,
                password=settings.reddit_password,
                user_agent=settings.reddit_user_agent,
            )
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _thing_from_fullname(self, fullname: str) -> Comment | Submission:
        prefix, _, thing_id = fullname.partition("_")
        if prefix == "t1":
            return self._reddit.comment(thing_id)
        if prefix == "t3":
            return self._reddit.submission(thing_id)
        raise ValueError(f"Unsupported fullname: {fullname}")

    @staticmethod
    def _format_user_reports(raw_reports: object) -> list[str]:
        if not isinstance(raw_reports, list):
            return []
        out: list[str] = []
        for entry in raw_reports:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                reason, count = entry[0], entry[1]
                out.append(f"{reason} x{count}")
                continue
            out.append(str(entry))
        return out

    @staticmethod
    def _format_mod_reports(raw_reports: object) -> list[str]:
        return RedditService._format_user_reports(raw_reports)

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        lowered = url.lower()
        if "i.redd.it/" in lowered:
            return True
        return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

    @classmethod
    def _extract_submission_media(cls, submission: Submission) -> tuple[str | None, str | None, str | None]:
        link_url = str(getattr(submission, "url", "") or "")
        link_url = link_url.strip() or None

        thumbnail_url: str | None = None
        raw_thumb = getattr(submission, "thumbnail", None)
        if isinstance(raw_thumb, str) and raw_thumb.startswith("http"):
            thumbnail_url = raw_thumb

        media_url: str | None = None
        preview = getattr(submission, "preview", None)
        if isinstance(preview, dict):
            images = preview.get("images")
            if isinstance(images, list) and images:
                source = images[0].get("source") if isinstance(images[0], dict) else None
                if isinstance(source, dict):
                    url = source.get("url")
                    if isinstance(url, str) and url:
                        media_url = html.unescape(url)

        if media_url is None and link_url and cls._looks_like_image_url(link_url):
            media_url = link_url

        return link_url, media_url, thumbnail_url

    def _fetch_reports_sync(self) -> list[ReportedItem]:
        subreddit = self._reddit.subreddit(self.settings.reddit_subreddit)
        items: list[ReportedItem] = []

        for thing in subreddit.mod.reports(limit=self.settings.max_reports_per_poll):
            link_url: str | None = None
            media_url: str | None = None
            thumbnail_url: str | None = None
            if isinstance(thing, Comment):
                kind = "comment"
                title = getattr(thing, "link_title", "Comment") or "Comment"
                body = getattr(thing, "body", "") or ""
                snippet = body
            elif isinstance(thing, Submission):
                kind = "submission"
                title = getattr(thing, "title", "Submission") or "Submission"
                body = getattr(thing, "selftext", "") or ""
                link_url, media_url, thumbnail_url = self._extract_submission_media(thing)
                if not body:
                    body = link_url or ""
                snippet = body
            else:
                continue

            author_obj = getattr(thing, "author", None)
            author = getattr(author_obj, "name", "[deleted]") if author_obj else "[deleted]"
            permalink = f"https://www.reddit.com{getattr(thing, 'permalink', '')}"
            created_utc = float(getattr(thing, "created_utc", time.time()))
            num_reports = int(getattr(thing, "num_reports", 0) or 0)
            fullname = str(getattr(thing, "name", ""))
            if not fullname:
                prefix = "t1" if kind == "comment" else "t3"
                fullname = f"{prefix}_{thing.id}"

            items.append(
                ReportedItem(
                    fullname=fullname,
                    kind=kind,
                    subreddit=str(getattr(thing.subreddit, "display_name", self.settings.reddit_subreddit)),
                    author=author,
                    permalink=permalink,
                    link_url=link_url,
                    media_url=media_url,
                    thumbnail_url=thumbnail_url,
                    title=_truncate(_squash_whitespace(title), 250),
                    snippet=_truncate(_squash_whitespace(snippet), 800),
                    num_reports=num_reports,
                    created_utc=created_utc,
                    locked=bool(getattr(thing, "locked", False)),
                    reports_ignored=bool(getattr(thing, "ignore_reports", False)),
                    removed=bool(
                        getattr(thing, "removed_by_category", None)
                        or getattr(thing, "banned_by", None)
                    ),
                    approved=bool(getattr(thing, "approved_by", None)),
                    user_reports=self._format_user_reports(getattr(thing, "user_reports", [])),
                    mod_reports=self._format_mod_reports(getattr(thing, "mod_reports", [])),
                )
            )

        items.sort(key=lambda item: item.created_utc)
        return items

    async def fetch_reports(self) -> list[ReportedItem]:
        return await self._run(self._fetch_reports_sync)

    def _approve_item_sync(self, fullname: str) -> None:
        thing = self._thing_from_fullname(fullname)
        thing.mod.approve()

    async def approve_item(self, fullname: str) -> None:
        await self._run(self._approve_item_sync, fullname)

    def _remove_item_sync(self, fullname: str, spam: bool, mod_note: str) -> None:
        thing = self._thing_from_fullname(fullname)
        kwargs: dict[str, object] = {"spam": spam}
        note = mod_note.strip()
        if note:
            kwargs["mod_note"] = note
        thing.mod.remove(**kwargs)

    async def remove_item(self, fullname: str, spam: bool, mod_note: str = "") -> None:
        await self._run(self._remove_item_sync, fullname, spam, mod_note)

    def _set_lock_sync(self, fullname: str, locked: bool) -> None:
        thing = self._thing_from_fullname(fullname)
        if locked:
            thing.mod.lock()
        else:
            thing.mod.unlock()

    async def set_lock(self, fullname: str, locked: bool) -> None:
        await self._run(self._set_lock_sync, fullname, locked)

    def _set_ignore_reports_sync(self, fullname: str, ignored: bool) -> None:
        thing = self._thing_from_fullname(fullname)
        if ignored:
            thing.mod.ignore_reports()
        else:
            thing.mod.unignore_reports()

    async def set_ignore_reports(self, fullname: str, ignored: bool) -> None:
        await self._run(self._set_ignore_reports_sync, fullname, ignored)

    def _get_report_details_sync(self, fullname: str) -> tuple[list[str], list[str]]:
        thing = self._thing_from_fullname(fullname)
        user_reports = self._format_user_reports(getattr(thing, "user_reports", []))
        mod_reports = self._format_mod_reports(getattr(thing, "mod_reports", []))
        return user_reports, mod_reports

    async def get_report_details(self, fullname: str) -> tuple[list[str], list[str]]:
        return await self._run(self._get_report_details_sync, fullname)

    def _ban_user_sync(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> None:
        subreddit = self._reddit.subreddit(subreddit_name)
        kwargs: dict[str, object] = {}
        if duration_days is not None:
            kwargs["duration"] = duration_days
        if ban_reason.strip():
            kwargs["ban_reason"] = ban_reason.strip()
        if mod_note.strip():
            kwargs["note"] = mod_note.strip()
        if ban_message.strip():
            kwargs["ban_message"] = ban_message.strip()

        try:
            subreddit.banned.add(username, **kwargs)
        except TypeError:
            kwargs.pop("ban_message", None)
            subreddit.banned.add(username, **kwargs)

    async def ban_user(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> None:
        await self._run(
            self._ban_user_sync,
            subreddit_name,
            username,
            duration_days,
            ban_reason,
            mod_note,
            ban_message,
        )

    def _send_modmail_sync(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> None:
        subreddit = self._reddit.subreddit(subreddit_name)
        subreddit.modmail.create(
            subject=subject.strip(),
            body=body.strip(),
            recipient=recipient.strip(),
            author_hidden=author_hidden,
        )

    async def send_modmail(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> None:
        await self._run(
            self._send_modmail_sync,
            subreddit_name,
            recipient,
            subject,
            body,
            author_hidden,
        )

    def _send_removal_message_sync(
        self,
        fullname: str,
        message_body: str,
        message_title: str,
        mod_note: str,
        public_as_subreddit: bool,
    ) -> None:
        thing = self._thing_from_fullname(fullname)
        remove_kwargs: dict[str, object] = {}
        if mod_note.strip():
            remove_kwargs["mod_note"] = mod_note.strip()
        thing.mod.remove(**remove_kwargs)
        message_type = "public_as_subreddit" if public_as_subreddit else "public"
        thing.mod.send_removal_message(
            message=message_body.strip(),
            title=message_title.strip() or "Removed",
            type=message_type,
        )

    async def send_removal_message(
        self,
        fullname: str,
        message_body: str,
        message_title: str,
        mod_note: str,
        public_as_subreddit: bool,
    ) -> None:
        await self._run(
            self._send_removal_message_sync,
            fullname,
            message_body,
            message_title,
            mod_note,
            public_as_subreddit,
        )

    def _reply_sync(self, fullname: str, body: str, sticky: bool, lock: bool) -> None:
        thing = self._thing_from_fullname(fullname)
        comment = thing.reply(body)
        try:
            if sticky:
                comment.mod.distinguish(sticky=True)
            else:
                comment.mod.distinguish()
        except Exception:
            pass
        if lock:
            try:
                if isinstance(thing, Submission):
                    thing.mod.lock()
                else:
                    thing.submission.mod.lock()
            except Exception:
                pass

    async def reply(self, fullname: str, body: str, sticky: bool, lock: bool) -> None:
        await self._run(self._reply_sync, fullname, body, sticky, lock)

    def _refresh_state_sync(self, fullname: str) -> dict[str, object]:
        thing = self._thing_from_fullname(fullname)
        # Accessing attrs triggers lazy refresh for these fields in PRAW.
        _ = thing.id
        return {
            "locked": bool(getattr(thing, "locked", False)),
            "reports_ignored": bool(getattr(thing, "ignore_reports", False)),
            "removed": bool(
                getattr(thing, "removed_by_category", None)
                or getattr(thing, "banned_by", None)
            ),
            "approved": bool(getattr(thing, "approved_by", None)),
            "num_reports": int(getattr(thing, "num_reports", 0) or 0),
        }

    async def refresh_state(self, fullname: str) -> dict[str, object]:
        return await self._run(self._refresh_state_sync, fullname)

    def _test_auth_sync(self) -> str:
        me = self._reddit.user.me()
        if me is None:
            raise RuntimeError("Unable to resolve authenticated Reddit user")
        subreddit = self._reddit.subreddit(self.settings.reddit_subreddit)
        _ = subreddit.display_name
        return f"Authenticated as u/{me.name}; subreddit r/{subreddit.display_name} reachable"

    async def test_auth(self) -> str:
        return await self._run(self._test_auth_sync)


def _main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    service = RedditService(settings)

    async def run_test() -> None:
        message = await service.test_auth()
        logger.info(message)

    asyncio.run(run_test())


if __name__ == "__main__":
    _main()
