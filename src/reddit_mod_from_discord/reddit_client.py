from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Protocol

import praw
from praw.models import Comment, Submission

from dotenv import load_dotenv

from reddit_mod_from_discord.config import ResolvedSettings, Settings, load_settings, resolve_settings
from reddit_mod_from_discord.models import ReportedItem
from reddit_mod_from_discord.safety import sanitize_http_url

logger = logging.getLogger("reddit_mod_from_discord")


class RedditApi(Protocol):
    async def fetch_reports(self) -> list[ReportedItem]: ...

    async def approve_item(self, fullname: str) -> None: ...

    async def remove_item(self, fullname: str, spam: bool, mod_note: str = "") -> None: ...

    async def set_lock(self, fullname: str, locked: bool) -> None: ...

    async def set_ignore_reports(self, fullname: str, ignored: bool) -> None: ...

    async def refresh_state(self, fullname: str) -> dict[str, object]: ...

    async def reply(self, fullname: str, body: str, sticky: bool, lock: bool) -> str | None: ...

    async def ban_user(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> str | None: ...

    async def send_modmail(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> str | None: ...

    async def send_removal_message(
        self,
        fullname: str,
        message_body: str,
        message_title: str,
        mod_note: str,
        public_as_subreddit: bool,
    ) -> None: ...

    async def fetch_recent_modlog_entries(
        self,
        subreddit_name: str,
        *,
        limit: int,
        min_created_utc: float | None = None,
    ) -> list[tuple[str, float, str]]: ...


def _squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


class RedditSettings(Protocol):
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_refresh_token: str | None
    reddit_username: str | None
    reddit_password: str | None
    reddit_user_agent: str
    reddit_subreddit: str | None
    max_reports_per_poll: int


class RedditService:
    def __init__(self, settings: RedditSettings) -> None:
        self.settings = settings
        if not settings.reddit_client_id or not settings.reddit_client_secret:
            raise ValueError("Missing Reddit app credentials")
        if not settings.reddit_subreddit:
            raise ValueError("Missing Reddit subreddit")
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
        self._bot_username: str | None = None

    @staticmethod
    def _validate_thing_id(thing_id: str) -> str:
        thing_id = thing_id.strip()
        if not thing_id:
            raise ValueError("Missing thing id")
        # Reddit thing IDs are base36; in practice they're lowercase but accept uppercase defensively.
        if re.fullmatch(r"[0-9a-zA-Z]+", thing_id) is None:
            raise ValueError(f"Invalid thing id: {thing_id!r}")
        return thing_id.lower()

    @staticmethod
    def _parse_reports(raw_reports: object) -> tuple[list[str], int]:
        if not isinstance(raw_reports, list):
            return [], 0
        lines: list[str] = []
        total = 0
        for entry in raw_reports:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                reason_raw = entry[0]
                count_raw = entry[1]
                reason = str(reason_raw).strip() or "Unknown reason"
                try:
                    count = int(count_raw)
                except Exception:
                    count = 1
                if count < 0:
                    count = 0
                lines.append(f"{reason} x{count}")
                total += count
                continue

            text = str(entry).strip()
            if not text:
                continue
            lines.append(text)
            total += 1
        return lines, total

    async def _run(self, fn, *args, **kwargs):
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    def _thing_from_fullname(self, fullname: str) -> Comment | Submission:
        prefix, _, thing_id = fullname.partition("_")
        thing_id = self._validate_thing_id(thing_id)
        if prefix == "t1":
            return self._reddit.comment(thing_id)
        if prefix == "t3":
            return self._reddit.submission(thing_id)
        raise ValueError(f"Unsupported fullname: {fullname}")

    @staticmethod
    def _format_user_reports(raw_reports: object) -> list[str]:
        lines, _ = RedditService._parse_reports(raw_reports)
        return lines

    @staticmethod
    def _format_mod_reports(raw_reports: object) -> list[str]:
        lines, _ = RedditService._parse_reports(raw_reports)
        return lines

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        lowered = url.lower()
        if "i.redd.it/" in lowered:
            return True
        return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

    @classmethod
    def _extract_submission_media(cls, submission: Submission) -> tuple[str | None, str | None, str | None]:
        raw_link_url = str(getattr(submission, "url", "") or "")
        link_url = sanitize_http_url(raw_link_url)

        thumbnail_url: str | None = None
        raw_thumb = getattr(submission, "thumbnail", None)
        if isinstance(raw_thumb, str):
            thumbnail_url = sanitize_http_url(raw_thumb)

        media_url: str | None = None
        preview = getattr(submission, "preview", None)
        if isinstance(preview, dict):
            images = preview.get("images")
            if isinstance(images, list) and images:
                source = images[0].get("source") if isinstance(images[0], dict) else None
                if isinstance(source, dict):
                    url = source.get("url")
                    if isinstance(url, str) and url:
                        media_url = sanitize_http_url(html.unescape(url))

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
            raw_permalink = f"https://www.reddit.com{getattr(thing, 'permalink', '')}"
            permalink = sanitize_http_url(raw_permalink) or f"https://www.reddit.com/r/{self.settings.reddit_subreddit}/"
            created_utc = float(getattr(thing, "created_utc", time.time()))
            num_reports = int(getattr(thing, "num_reports", 0) or 0)
            fullname = str(getattr(thing, "name", ""))
            if not fullname:
                prefix = "t1" if kind == "comment" else "t3"
                fullname = f"{prefix}_{thing.id}"

            user_reports, user_total = self._parse_reports(getattr(thing, "user_reports", []))
            mod_reports, mod_total = self._parse_reports(getattr(thing, "mod_reports", []))
            computed_total = user_total + mod_total
            if (num_reports <= 0 or num_reports < computed_total) and computed_total > 0:
                num_reports = computed_total

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
                    user_reports=user_reports,
                    mod_reports=mod_reports,
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

    def _ban_user_sync(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> str | None:
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
        return f"https://www.reddit.com/r/{subreddit_name}/about/log/?type=banuser"

    async def ban_user(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> str | None:
        return await self._run(
            self._ban_user_sync,
            subreddit_name,
            username,
            duration_days,
            ban_reason,
            mod_note,
            ban_message,
        )

    @staticmethod
    def _modmail_url_from_object(conversation: object) -> str | None:
        conv_id: str | None = None

        raw_id = getattr(conversation, "id", None)
        if isinstance(raw_id, str) and raw_id:
            conv_id = raw_id

        if conv_id is None and isinstance(conversation, dict):
            raw = conversation.get("id")
            if isinstance(raw, str) and raw:
                conv_id = raw
            if conv_id is None:
                nested = conversation.get("conversation")
                if isinstance(nested, dict):
                    raw_nested = nested.get("id")
                    if isinstance(raw_nested, str) and raw_nested:
                        conv_id = raw_nested

        if not conv_id:
            return None
        return f"https://mod.reddit.com/mail/perma/{conv_id}"

    def _send_modmail_sync(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> str | None:
        subreddit = self._reddit.subreddit(subreddit_name)
        conversation = subreddit.modmail.create(
            subject=subject.strip(),
            body=body.strip(),
            recipient=recipient.strip(),
            author_hidden=author_hidden,
        )
        return self._modmail_url_from_object(conversation)

    async def send_modmail(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> str | None:
        return await self._run(
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

    def _reply_sync(self, fullname: str, body: str, sticky: bool, lock: bool) -> str | None:
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
        permalink = getattr(comment, "permalink", None)
        if isinstance(permalink, str) and permalink:
            return sanitize_http_url(f"https://www.reddit.com{permalink}")
        return None

    async def reply(self, fullname: str, body: str, sticky: bool, lock: bool) -> str | None:
        return await self._run(self._reply_sync, fullname, body, sticky, lock)

    def _refresh_state_sync(self, fullname: str) -> dict[str, object]:
        thing = self._thing_from_fullname(fullname)
        # Accessing attrs triggers lazy refresh for these fields in PRAW.
        _ = thing.id
        try:
            raw_num_reports = int(getattr(thing, "num_reports", 0) or 0)
        except Exception:
            raw_num_reports = 0
        return {
            "locked": bool(getattr(thing, "locked", False)),
            "reports_ignored": bool(getattr(thing, "ignore_reports", False)),
            "removed": bool(
                getattr(thing, "removed_by_category", None)
                or getattr(thing, "banned_by", None)
            ),
            "approved": bool(getattr(thing, "approved_by", None)),
            "num_reports": max(0, raw_num_reports),
        }

    async def refresh_state(self, fullname: str) -> dict[str, object]:
        return await self._run(self._refresh_state_sync, fullname)

    def _format_modlog_entry(self, action) -> str:
        action_name = str(getattr(action, "action", "") or "unknown")
        mod = getattr(action, "mod", None)
        mod_name = "unknown"
        if mod is not None:
            mod_name = getattr(mod, "name", None) or str(mod)
        created_raw = getattr(action, "created_utc", None)
        if isinstance(created_raw, (int, float)):
            stamp = datetime.fromtimestamp(float(created_raw), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        else:
            stamp = "unknown time"
        details = getattr(action, "details", None)
        extra = ""
        if details:
            extra = f" ({details})"
        return f"modlog: {action_name} by u/{mod_name} at {stamp}{extra}"

    def _resolve_bot_username(self) -> str | None:
        if self._bot_username is not None:
            return self._bot_username or None
        try:
            me = self._reddit.user.me()
        except Exception:
            self._bot_username = ""
            return None
        if me is None:
            self._bot_username = ""
            return None
        name = getattr(me, "name", None)
        if not name:
            self._bot_username = ""
            return None
        self._bot_username = str(name)
        return self._bot_username

    def _fetch_recent_modlog_entries_sync(
        self,
        subreddit_name: str,
        *,
        limit: int,
        min_created_utc: float | None = None,
    ) -> list[tuple[str, float, str]]:
        subreddit = self._reddit.subreddit(subreddit_name)
        bot_username = self._resolve_bot_username()
        bot_username_norm = bot_username.lower() if bot_username else None
        entries: list[tuple[str, float, str]] = []
        for action in subreddit.mod.log(limit=limit):
            created_raw = getattr(action, "created_utc", None)
            if isinstance(created_raw, (int, float)):
                created_utc = float(created_raw)
            else:
                created_utc = 0.0
            if min_created_utc is not None and created_utc and created_utc < min_created_utc:
                break
            if bot_username_norm:
                mod = getattr(action, "mod", None)
                mod_name = getattr(mod, "name", None) or (str(mod) if mod is not None else "")
                if mod_name and mod_name.lower() == bot_username_norm:
                    continue
            target_fullname = getattr(action, "target_fullname", None)
            if not isinstance(target_fullname, str) or not target_fullname:
                continue
            if not target_fullname.startswith(("t1_", "t3_")):
                continue
            line = self._format_modlog_entry(action)
            entries.append((target_fullname, created_utc, line))
        return entries

    async def fetch_recent_modlog_entries(
        self,
        subreddit_name: str,
        *,
        limit: int,
        min_created_utc: float | None = None,
    ) -> list[tuple[str, float, str]]:
        return await self._run(
            self._fetch_recent_modlog_entries_sync,
            subreddit_name,
            limit=limit,
            min_created_utc=min_created_utc,
        )

    def _test_auth_sync(self) -> str:
        me = self._reddit.user.me()
        if me is None:
            raise RuntimeError("Unable to resolve authenticated Reddit user")
        subreddit = self._reddit.subreddit(self.settings.reddit_subreddit)
        _ = subreddit.display_name
        return f"Authenticated as u/{me.name}; subreddit r/{subreddit.display_name} reachable"

    async def test_auth(self) -> str:
        return await self._run(self._test_auth_sync)


class DemoRedditService:
    def __init__(self) -> None:
        self._state: dict[str, dict[str, object]] = {}
        self._reports: dict[str, tuple[list[str], list[str]]] = {}

    def seed(
        self,
        fullname: str,
        *,
        num_reports: int = 1,
        user_reports: list[str] | None = None,
        mod_reports: list[str] | None = None,
    ) -> None:
        self._state.setdefault(
            fullname,
            {
                "locked": False,
                "reports_ignored": False,
                "removed": False,
                "approved": False,
                "num_reports": num_reports,
            },
        )
        if user_reports is None:
            user_reports = ["Spam x1"]
        if mod_reports is None:
            mod_reports = []
        self._reports[fullname] = (list(user_reports), list(mod_reports))

    async def fetch_reports(self) -> list[ReportedItem]:
        return []

    async def approve_item(self, fullname: str) -> None:
        state = self._state.setdefault(fullname, {})
        state["approved"] = True
        state["removed"] = False

    async def remove_item(self, fullname: str, spam: bool, mod_note: str = "") -> None:
        state = self._state.setdefault(fullname, {})
        state["removed"] = True
        state["approved"] = False

    async def set_lock(self, fullname: str, locked: bool) -> None:
        state = self._state.setdefault(fullname, {})
        state["locked"] = locked

    async def set_ignore_reports(self, fullname: str, ignored: bool) -> None:
        state = self._state.setdefault(fullname, {})
        state["reports_ignored"] = ignored

    async def refresh_state(self, fullname: str) -> dict[str, object]:
        return dict(self._state.get(fullname, {}))

    async def reply(self, fullname: str, body: str, sticky: bool, lock: bool) -> str | None:
        stamp = int(time.time())
        return f"https://www.reddit.com/comments/demo/{stamp}"

    async def ban_user(
        self,
        subreddit_name: str,
        username: str,
        duration_days: int | None,
        ban_reason: str,
        mod_note: str,
        ban_message: str,
    ) -> str | None:
        return f"https://www.reddit.com/r/{subreddit_name}/about/log/?type=banuser"

    async def send_modmail(
        self,
        subreddit_name: str,
        recipient: str,
        subject: str,
        body: str,
        author_hidden: bool,
    ) -> str | None:
        stamp = int(time.time())
        return f"https://mod.reddit.com/mail/perma/demo-{stamp}"

    async def send_removal_message(
        self,
        fullname: str,
        message_body: str,
        message_title: str,
        mod_note: str,
        public_as_subreddit: bool,
    ) -> None:
        return None

    async def fetch_recent_modlog_entries(
        self,
        subreddit_name: str,
        *,
        limit: int,
        min_created_utc: float | None = None,
    ) -> list[tuple[str, float, str]]:
        return []


def _main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    setup_id = (os.getenv("TEST_SETUP_ID") or "").strip()
    if settings.multi_server_config:
        if setup_id:
            setup = settings.multi_server_config.get(setup_id)
            if setup is None:
                raise RuntimeError(f"Unknown TEST_SETUP_ID: {setup_id}")
        else:
            if len(settings.multi_server_config) != 1:
                raise RuntimeError(
                    "TEST_SETUP_ID is required when MULTI_SERVER_CONFIG_PATH has multiple setups"
                )
            setup = next(iter(settings.multi_server_config.values()))
            setup_id = setup.setup_id
        resolved = resolve_settings(settings, setup.overrides)
    else:
        resolved = resolve_settings(settings, None)
    service = RedditService(resolved)

    async def run_test() -> None:
        message = await service.test_auth()
        logger.info(message)

    asyncio.run(run_test())


if __name__ == "__main__":
    _main()
