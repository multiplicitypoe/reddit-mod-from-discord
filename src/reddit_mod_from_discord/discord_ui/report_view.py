from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord

from reddit_mod_from_discord.discord_ui.reddit_card import render_reddit_card
from reddit_mod_from_discord.models import ReportViewPayload
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import RedditApi
from reddit_mod_from_discord.removal_reasons import RemovalReason, RemovalReasonSet, render_removal_message
from reddit_mod_from_discord.safety import sanitize_http_url
from reddit_mod_from_discord.store import BotStore, ViewRecord

logger = logging.getLogger("reddit_mod_from_discord")

# Every button here answers straight away and finishes the job afterwards, which
# only works if the background half is held onto. The event loop keeps weak
# references to tasks, so one whose only reference is a local variable can be
# collected part way through and cancelled. That arrives as a BaseException, so
# the usual except Exception never sees it and the done callback returns early
# on t.cancelled(). The same shape in the incident assistant meant its audit log
# summary never once reached a card, without a single line in the log.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro, what: str) -> asyncio.Task:
    """Run something after the button has answered, and keep hold of it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(finished: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(finished)
        if finished.cancelled():
            logger.warning("Background work was cancelled: %s", what)
            return
        error = finished.exception()
        if error is not None:
            logger.error("Background work failed: %s: %r", what, error)

    task.add_done_callback(_done)
    return task

# Modlog entries to pull when Mark Handled is pressed. Only entries for this one
# item are kept, but the API returns the subreddit's recent log, so this needs to
# be deep enough to still contain an action taken a few minutes ago on a busy
# subreddit.
_HANDLED_MODLOG_LIMIT = 100

_BAN_REASON_API_MAX = 100
_BAN_NOTE_API_MAX = 300
# Where the moderators are. Override with DISPLAY_TIMEZONE if that changes again.
_DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "America/New_York"))


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _format_timestamp(ts: float) -> str:
    if ts <= 0:
        return "unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _relative_age(ts: float) -> str:
    if ts <= 0:
        return "unknown"
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        minutes = delta // 60
        return f"{minutes}m ago"
    if delta < 86400:
        hours = delta // 3600
        return f"{hours}h ago"
    days = delta // 86400
    return f"{days}d ago"

_REPORT_COUNT_RE = re.compile(r"^(?P<reason>.*) x(?P<count>\d+)$")
_LEGACY_REPORT_LINE_RE = re.compile(
    r"""^\s*[\[(]\s*['"]?(?P<reason>.+?)['"]?\s*,\s*(?P<count>-?\d+)\s*[\])]\s*$"""
)
_MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)")
_UTC_STAMP_WITH_DATE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<hour>\d{2}):(?P<minute>\d{2}) UTC - (?P<rest>.+)$"
)
_UTC_STAMP_NO_DATE_RE = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}) UTC - (?P<rest>.+)$"
)
_LOCAL_STAMP_RE = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}) (?P<tz>PST|PDT|EST|EDT) - (?P<rest>.+)$"
)
# Offsets from UTC for the zones a stored line can carry, so an old stamp can be
# read back and re-rendered wherever the display zone now points.
_LOCAL_STAMP_OFFSETS = {"PST": -8, "PDT": -7, "EST": -5, "EDT": -4}
_MODLOG_ACTION_RE = re.compile(r"^u/(?P<mod>[^:]+): (?P<action>.+)$")
_CONFIRM_SUFFIX_RE = re.compile(r"\s*\((confirm_ham|confirm_spam)\)\s*$")


def _format_local_hhmm(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_DISPLAY_TZ)
    return dt.strftime("%H:%M %Z")


def _utc_stamp(now_utc: datetime = None) -> str:
    """The storage format for audit log lines.

    UTC with a full date, so the line stays true if the display zone changes and
    so a dateless stamp never has to be guessed back onto a day.
    """
    dt = now_utc if now_utc is not None else datetime.now(tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _nearest_utc_for_hhmm(
    hour: int, minute: int, now_utc: datetime, offset_hours: int = 0
) -> datetime:
    """Resolve a dateless HH:MM to the nearest day, given its offset from UTC."""
    candidates = []
    for day_offset in (0, -1, 1):
        midnight = (now_utc + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        candidates.append(
            midnight + timedelta(hours=hour - offset_hours, minutes=minute)
        )
    return min(candidates, key=lambda dt: abs((now_utc - dt).total_seconds()))


# Every action name this subreddit's modlog produces, mapped to what a moderator
# reads on the card. An unmapped action falls through as Reddit spells it.
_MODLOG_ACTION_NAMES = {
    "approvelink": "approved",
    "approvecomment": "approved",
    "removecomment": "removed",
    "removelink": "removed",
    "spamcomment": "removed as spam",
    "spamlink": "removed as spam",
    "lock": "locked",
    "unlock": "unlocked",
    "ignorereports": "ignored reports",
    "unignorereports": "unignored reports",
    "snoozereports": "snoozed reports",
    "addremovalreason": "added removal reason",
    "sticky": "stickied",
    "unsticky": "unstickied",
    "distinguish": "distinguished",
    "spoiler": "marked spoiler",
    "unspoiler": "unmarked spoiler",
    "marknsfw": "marked NSFW",
    "editflair": "edited flair",
    "setsuggestedsort": "set suggested sort",
    "submit_scheduled_post": "posted a scheduled post",
}

# Details worth dropping: they either repeat the action back or are an internal
# id. Anything else names the rule or reason behind the action and is kept.
_UNINFORMATIVE_DETAILS = {"remove", "unspam", "spam", "confirm_ham", "confirm_spam"}

# The detail is the final parenthesised group. It can itself contain parentheses,
# so this anchors on the last one rather than the first.
_TRAILING_DETAIL_RE = re.compile(r"^(?P<action>.*?)\s*\((?P<detail>.*)\)$", re.DOTALL)


def _normalize_modlog_action_text(action_text: str) -> str:
    text = str(action_text).replace("[modlog]", " ").strip()
    text = _CONFIRM_SUFFIX_RE.sub("", text).strip()

    detail = ""
    match = _TRAILING_DETAIL_RE.match(text)
    if match is not None:
        text = match.group("action").strip()
        detail = match.group("detail").strip()

    name = _MODLOG_ACTION_NAMES.get(text.lower(), text)
    if detail.lower() in _UNINFORMATIVE_DETAILS or detail.isdigit():
        detail = ""
    return "{} ({})".format(name, detail) if detail else name


def _normalize_audit_log_entry(line: str, now_utc: datetime = None) -> str:
    """
    Normalize stored audit log lines for display:
    - Always render timestamp as HH:MM in the display timezone.
    - Collapse modlog entries into a similar shape as in-bot actions.
    """
    raw = str(line).strip()
    if not raw:
        return raw

    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    local_match = _LOCAL_STAMP_RE.match(raw)
    if local_match is not None:
        # Stamped in local time when it was stored. Read it back through its own
        # offset so it lands in the display zone, which may not be the zone it
        # was written in. A line already in the display zone survives unchanged.
        dt_utc = _nearest_utc_for_hhmm(
            int(local_match.group("hour")),
            int(local_match.group("minute")),
            now_utc,
            _LOCAL_STAMP_OFFSETS[local_match.group("tz")],
        )
        return "{} - {}".format(
            _format_local_hhmm(dt_utc.timestamp()), local_match.group("rest").strip()
        )

    match = _UTC_STAMP_WITH_DATE_RE.match(raw)
    if match is not None:
        dt_utc = datetime.strptime(
            f"{match.group('date')} {match.group('hour')}:{match.group('minute')}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=timezone.utc)
        stamp = _format_local_hhmm(dt_utc.timestamp())
        rest = match.group("rest").strip()
    else:
        match = _UTC_STAMP_NO_DATE_RE.match(raw)
        if match is None:
            return raw
        dt_utc = _nearest_utc_for_hhmm(
            int(match.group("hour")), int(match.group("minute")), now_utc
        )
        stamp = _format_local_hhmm(dt_utc.timestamp())
        rest = match.group("rest").strip()

    # If the remainder looks like a modlog action, normalize its action name and drop confirm_*.
    modlog_match = _MODLOG_ACTION_RE.match(rest)
    if modlog_match is not None:
        mod = modlog_match.group("mod").strip()
        action_text = modlog_match.group("action").strip()
        action_text = _normalize_modlog_action_text(action_text)
        return f"{stamp} - u/{mod}: {action_text}"

    # Otherwise, keep the message content but render local timestamp.
    return f"{stamp} - {rest}"


def _escape_discord_text(text: str) -> str:
    escaped = discord.utils.escape_markdown(str(text))
    return discord.utils.escape_mentions(escaped)


def _normalize_report_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        legacy = _LEGACY_REPORT_LINE_RE.match(text)
        if legacy is not None:
            reason = legacy.group("reason").strip().strip("'\"") or "Unknown reason"
            try:
                count = int(legacy.group("count"))
            except Exception:
                count = 1
            if count < 0:
                count = 0
            out.append(f"{reason} x{count}")
            continue
        out.append(text)
    return out


def _format_audit_log_line(text: str) -> str:
    # Preserve explicit markdown links while escaping everything else.
    source = str(text)
    parts: list[str] = []
    cursor = 0
    for match in _MARKDOWN_LINK_RE.finditer(source):
        start, end = match.span()
        if start > cursor:
            parts.append(_escape_discord_text(source[cursor:start]))

        raw_label = match.group("label")
        raw_url = match.group("url")
        safe_url = sanitize_http_url(raw_url)
        if safe_url:
            safe_label = _escape_discord_text(raw_label)
            parts.append(f"[{safe_label}]({safe_url})")
        else:
            parts.append(_escape_discord_text(source[start:end]))
        cursor = end

    if cursor < len(source):
        parts.append(_escape_discord_text(source[cursor:]))
    return "".join(parts)


def _sum_report_counts(lines: list[str]) -> int:
    total = 0
    for line in lines:
        m = _REPORT_COUNT_RE.match(line.strip())
        if not m:
            continue
        try:
            total += int(m.group("count"))
        except Exception:
            continue
    return total


def _aggregate_reports(lines: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for line in _normalize_report_lines(lines):
        m = _REPORT_COUNT_RE.match(line.strip())
        if m:
            reason = m.group("reason").strip() or "Unknown reason"
            try:
                count = int(m.group("count"))
            except Exception:
                count = 1
        else:
            reason = line.strip()
            count = 1
        if count < 0:
            count = 0
        counts[reason] = counts.get(reason, 0) + count

    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [f"{_escape_discord_text(reason)} x{count}" for reason, count in items if reason]


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _context_permalink(permalink: str, kind: str) -> str:
    """Append Reddit's context param so the link opens the full thread
    around a comment rather than just that one isolated reply. Meaningless
    for a submission link, so left untouched there."""
    if kind != "comment" or not permalink:
        return permalink
    sep = "&" if "?" in permalink else "?"
    return f"{permalink}{sep}context=1000"


def build_report_embed(payload: ReportViewPayload, *, has_card: bool = False) -> discord.Embed:
    thing_label = "Post" if payload.kind == "submission" else "Comment"
    if payload.handled:
        color = discord.Color.green()
    elif payload.removed:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()
    safe_permalink = sanitize_http_url(payload.permalink)
    safe_media_url = sanitize_http_url(payload.media_url)
    safe_thumbnail_url = sanitize_http_url(payload.thumbnail_url)
    safe_link_url = sanitize_http_url(payload.link_url)
    embed_url = _context_permalink(safe_permalink, payload.kind) if safe_permalink else safe_permalink

    subreddit = _escape_discord_text(payload.subreddit)
    author = _escape_discord_text(payload.author or "[deleted]")
    title = f"Reported {thing_label} in /r/{subreddit} by {author}"
    embed = discord.Embed(title=_truncate(title, 256), color=color, url=embed_url)
    summary = _escape_discord_text(payload.title if payload.title else thing_label)
    status: list[str] = []
    if payload.approved:
        status.append("approved")
    if payload.removed:
        status.append("removed")
    if payload.locked:
        status.append("locked")
    if payload.reports_ignored:
        status.append("ignored")
    if payload.handled:
        status.append("handled")
    if status:
        status_value = ", ".join(status)
    else:
        status_value = "active"

    # The reported text ("snippet") is computed once; how it's shown differs
    # by kind below, since a comment's own words are what got reported, while
    # a submission's title already is the headline.
    snippet_text = ""
    if payload.snippet:
        raw_snippet = payload.snippet.strip()
        safe_snippet = sanitize_http_url(raw_snippet)
        if safe_snippet and safe_snippet in {safe_link_url, safe_permalink, safe_media_url}:
            raw_snippet = ""
        if raw_snippet:
            snippet_text = _truncate(_escape_discord_text(raw_snippet), 900)

    link_line = None
    if (
        safe_link_url
        and safe_link_url != safe_permalink
        and safe_link_url != safe_media_url
    ):
        link_line = f"**Link:** {safe_link_url}"

    if payload.kind == "submission":
        description_lines = [f"**Title:** {_truncate(summary, 300)}"]
        description_lines.append(f"**Status:** {status_value}")
        if payload.num_comments is not None:
            description_lines.append(f"**Comments:** {payload.num_comments}")
        if link_line:
            description_lines.append(link_line)
        if snippet_text:
            description_lines.append(f"**Text:** {snippet_text}")
    elif has_card:
        # A rendered Reddit-style card carries the comment, its context, and
        # the post title, so the description only needs the state that isn't
        # visual.
        description_lines = [f"**Status:** {status_value}"]
        if link_line:
            description_lines.append(link_line)
    else:
        # Fallback for when the card couldn't be rendered (e.g. fonts
        # unavailable): same text-only layout as before the image existed.
        # Discord doesn't dim italic text, so the post title gets its own
        # field rather than a same-brightness "On: title" line that would
        # out-weigh a short blockquote by sheer length.
        description_lines = []
        if snippet_text:
            description_lines.append(
                "\n".join(f"> {line}" for line in snippet_text.splitlines())
            )
        else:
            description_lines.append("_(comment text unavailable)_")
        description_lines.append("")
        description_lines.append(f"**Status:** {status_value}")
        if link_line:
            description_lines.append(link_line)

    embed.description = "\n".join(description_lines)

    if not has_card:
        if safe_media_url:
            embed.set_image(url=safe_media_url)
        elif safe_thumbnail_url:
            embed.set_thumbnail(url=safe_thumbnail_url)

        if payload.kind != "submission":
            embed.add_field(
                name="Reported comment on",
                value=_truncate(summary, 300),
                inline=False,
            )

    user_reports = _normalize_report_lines(payload.user_reports)
    mod_reports = _normalize_report_lines(payload.mod_reports)

    all_reports = _aggregate_reports(user_reports + mod_reports)
    report_lines: list[str] = []
    if all_reports:
        report_lines.extend([f"- {line}" for line in all_reports[:10]])
    if not report_lines:
        report_lines = ["No report reason text returned by Reddit."]
    embed.add_field(
        name="Report reasons",
        value=_truncate("\n".join(report_lines), 1024),
        inline=False,
    )

    if payload.action_log:
        normalized_audit = [
            _normalize_audit_log_entry(line) for line in payload.action_log[-10:]
        ]
        escaped_audit = [_format_audit_log_line(line) for line in normalized_audit]
        embed.add_field(
            name="Audit Log",
            value=_truncate("\n".join(f"- {line}" for line in escaped_audit), 1024),
            inline=False,
        )

    if payload.created_utc > 0:
        embed.set_footer(text=f"Posted {_relative_age(payload.created_utc)}")
    return embed


def build_report_attachment(payload: ReportViewPayload) -> discord.File | None:
    """The rendered Reddit-style card for a comment report, as a file with
    real accessible alt text. Returns None for submissions (nothing to
    render beyond the title, which the embed already shows) and whenever
    rendering fails for any reason — callers fall back to the text-only
    embed in that case, so a bad render never blocks report delivery."""
    if payload.kind != "comment":
        return None
    png_bytes = render_reddit_card(payload)
    if png_bytes is None:
        return None

    alt_parts = [f"Comment by u/{payload.author or '[deleted]'}"]
    if payload.parent_author:
        alt_parts.append(
            f", replying to u/{payload.parent_author}: "
            f"“{_truncate(payload.parent_body or '', 200)}”"
        )
    alt_parts.append(f", on “{_truncate(payload.title, 200)}” in r/{payload.subreddit}")
    if payload.post_is_self is True:
        if payload.post_selftext:
            alt_parts.append(f" (text post: “{_truncate(payload.post_selftext, 150)}”)")
        else:
            alt_parts.append(" (text post, no body)")
    elif payload.post_is_self is False:
        domain = None
        if payload.link_url:
            netloc = urlparse(payload.link_url).netloc
            domain = netloc[4:] if netloc.startswith("www.") else netloc or None
        alt_parts.append(f" (link post to {domain})" if domain else " (link post)")
    alt_parts.append(f": “{_truncate(payload.snippet, 400)}”")
    alt_text = _truncate("".join(alt_parts), 1024)

    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", payload.fullname) or "report"
    filename = f"report-{safe_id}.png"
    return discord.File(io.BytesIO(png_bytes), filename=filename, description=alt_text)


def build_report_message(payload: ReportViewPayload) -> tuple[discord.Embed, discord.File | None]:
    """The embed plus its optional Reddit-card attachment, built together so
    the embed always correctly reflects whether a card is actually coming.

    The card is deliberately NOT wired into the embed's own image slot.
    Discord always renders an embed's image after its description and
    fields, with no way to move it earlier - so referencing it there would
    put the actual reported content last, under a screen of metadata. A
    plain attachment that no embed references renders above every embed in
    the message instead, which is the order that actually matters here:
    see the content first, read the status/reasons/buttons after."""
    attachment = build_report_attachment(payload)
    embed = build_report_embed(payload, has_card=attachment is not None)
    return embed, attachment


@dataclass(frozen=True)
class MessageRef:
    message_id: int
    channel_id: int
    guild_id: int


class BanModal(discord.ui.Modal, title="Ban User"):
    username = discord.ui.TextInput(
        label="Reddit Username",
        placeholder="without /u/",
        required=True,
        max_length=64,
    )
    duration_days = discord.ui.TextInput(
        label="Duration in days (blank = permanent)",
        placeholder="e.g. 7",
        required=False,
        max_length=3,
    )
    ban_reason = discord.ui.TextInput(
        label="Ban Reason (not sent to user)",
        required=False,
        max_length=300,
    )
    ban_message = discord.ui.TextInput(
        label="Message to user",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
    )

    def __init__(self, view: "ReportView", message_ref: MessageRef, default_username: str) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref
        if default_username and default_username not in {"[deleted]", ""}:
            self.username.default = default_username

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._view.ensure_mod_from_modal(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        raw_duration = str(self.duration_days.value or "").strip()
        duration_days: int | None = None
        if raw_duration:
            try:
                duration_days = int(raw_duration)
            except ValueError:
                await interaction.followup.send("Duration must be an integer number of days.", ephemeral=True)
                return
            if duration_days <= 0:
                await interaction.followup.send("Duration must be greater than 0.", ephemeral=True)
                return

        username = str(self.username.value).strip().removeprefix("u/").removeprefix("/u/")
        if not username:
            await interaction.followup.send("Username is required.", ephemeral=True)
            return

        action_start = time.monotonic()
        try:
            reason = str(self.ban_reason.value or "").strip()
            modlog_url = await self._view.reddit.ban_user(
                subreddit_name=self._view.payload.subreddit,
                username=username,
                duration_days=duration_days,
                ban_reason=reason[:_BAN_REASON_API_MAX],
                mod_note=reason[:_BAN_NOTE_API_MAX],
                ban_message=str(self.ban_message.value or "").strip(),
            )
        except Exception as exc:
            logger.exception("Ban action failed")
            await interaction.followup.send(f"Ban failed: {exc}", ephemeral=True)
            return
        action_s = time.monotonic() - action_start

        duration_label = f"{duration_days}d" if duration_days else "permanent"
        if modlog_url:
            action_text = f"banned u/{username} ({duration_label}) ([mod log]({modlog_url}))"
        else:
            action_text = f"banned u/{username} ({duration_label})"
        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            action_text,
            action_duration_s=action_s,
        )


class RemovalMessageModal(discord.ui.Modal, title="Removal Message"):
    title_text = discord.ui.TextInput(
        label="Short title (max 50 chars)",
        required=False,
        max_length=50,
    )
    mod_note = discord.ui.TextInput(
        label="Mod note on removal",
        required=False,
        max_length=250,
    )
    body = discord.ui.TextInput(
        label="Removal message body",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(
        self,
        view: "ReportView",
        message_ref: MessageRef,
        *,
        default_title: str | None = None,
        default_mod_note: str | None = None,
        default_body: str | None = None,
    ) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref
        if default_title is not None:
            self.title_text.default = str(default_title)[:50]
        if default_mod_note is not None:
            self.mod_note.default = str(default_mod_note)[:250]
        if default_body is not None:
            self.body.default = str(default_body)[:4000]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._view.ensure_mod_from_modal(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        body = str(self.body.value or "").strip()
        if not body:
            await interaction.followup.send("Message body is required.", ephemeral=True)
            return

        action_start = time.monotonic()
        try:
            await self._view.reddit.send_removal_message(
                fullname=self._view.payload.fullname,
                message_body=body,
                message_title=str(self.title_text.value or "").strip(),
                mod_note=str(self.mod_note.value or "").strip(),
                public_as_subreddit=True,
            )
        except Exception as exc:
            logger.exception("Removal message action failed")
            await interaction.followup.send(f"Removal message failed: {exc}", ephemeral=True)
            return
        action_s = time.monotonic() - action_start

        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            "removed + sent removal message as subreddit",
            action_duration_s=action_s,
        )


class ModmailModal(discord.ui.Modal, title="Send Modmail"):
    recipient = discord.ui.TextInput(
        label="Recipient username",
        placeholder="without /u/",
        required=True,
        max_length=64,
    )
    subject = discord.ui.TextInput(
        label="Subject",
        required=True,
        max_length=120,
    )
    body = discord.ui.TextInput(
        label="Body",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(self, view: "ReportView", message_ref: MessageRef, default_recipient: str) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref
        if default_recipient and default_recipient not in {"[deleted]", ""}:
            self.recipient.default = default_recipient

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._view.ensure_mod_from_modal(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        recipient = str(self.recipient.value or "").strip().removeprefix("u/").removeprefix("/u/")
        subject = str(self.subject.value or "").strip()
        body = str(self.body.value or "").strip()

        if not recipient or not subject or not body:
            await interaction.followup.send("Recipient, subject, and body are required.", ephemeral=True)
            return

        action_start = time.monotonic()
        try:
            modmail_url = await self._view.reddit.send_modmail(
                subreddit_name=self._view.payload.subreddit,
                recipient=recipient,
                subject=subject,
                body=body,
                author_hidden=True,
            )
        except Exception as exc:
            logger.exception("Modmail action failed")
            await interaction.followup.send(f"Modmail failed: {exc}", ephemeral=True)
            return
        action_s = time.monotonic() - action_start

        if modmail_url:
            action_text = f"sent a [modmail]({modmail_url}) to u/{recipient}"
        else:
            action_text = f"sent a modmail to u/{recipient}"

        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            action_text,
            action_duration_s=action_s,
        )


class ReplyModal(discord.ui.Modal, title="Reply"):
    remove_first = discord.ui.TextInput(
        label="Remove first? (y/n)",
        required=False,
        max_length=1,
        default="y",
    )
    sticky = discord.ui.TextInput(
        label="Sticky? (posts only) (y/n)",
        required=False,
        max_length=1,
        default="y",
    )
    lock = discord.ui.TextInput(
        label="Lock thread after? (y/n)",
        required=False,
        max_length=1,
        default="n",
    )
    body = discord.ui.TextInput(
        label="Reply body (sent as mod account)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(
        self,
        view: "ReportView",
        message_ref: MessageRef,
        *,
        default_remove_first: str | None = None,
        default_sticky: str | None = None,
        default_lock: str | None = None,
        default_body: str | None = None,
    ) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref
        if default_remove_first is not None:
            self.remove_first.default = str(default_remove_first)[:1]
        if default_sticky is not None:
            self.sticky.default = str(default_sticky)[:1]
        if default_lock is not None:
            self.lock.default = str(default_lock)[:1]
        if default_body is not None:
            self.body.default = str(default_body)[:4000]


def _truncate_select_label(text: str, max_len: int = 100) -> str:
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


class RemovalReasonSelect(discord.ui.Select):
    def __init__(self, picker: "RemovalReasonPickerView") -> None:
        self._picker = picker
        options = picker.build_options()
        super().__init__(
            placeholder="Select a removal reason…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._picker.report_view._ensure_mod(interaction):
            return
        value = self.values[0]
        try:
            idx = int(value)
        except Exception:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return
        await self._picker.select_index(interaction, idx)


class RemovalReasonPickerView(discord.ui.View):
    def __init__(
        self,
        *,
        report_view: "ReportView",
        message_ref: MessageRef,
        reason_set: RemovalReasonSet,
        reasons: list[RemovalReason],
        selected_index: int | None = None,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=10 * 60)
        self.report_view = report_view
        self.message_ref = message_ref
        self.reason_set = reason_set
        self.reasons = reasons
        self.selected_index = selected_index
        self.page = page
        self.page_size = 25

        self.select = RemovalReasonSelect(self)
        if not self.reasons:
            self.select.disabled = True
        self.add_item(self.select)

        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= len(self.reasons)
        self.open_button.disabled = self.selected_index is None
        self.back_button.disabled = self.selected_index is None
        if self.report_view.payload.kind == "submission":
            self.open_button.label = "Send removal message"
        else:
            self.open_button.label = "Send reply"
        if not self.reasons:
            self.prev_button.disabled = True
            self.next_button.disabled = True
            self.open_button.disabled = True
            self.back_button.disabled = True

        # On the preview screen, paging is confusing; require going "Back to list" first.
        if self.selected_index is not None:
            self.prev_button.disabled = True
            self.next_button.disabled = True

        self.prev_button.label = "Prev page"
        self.next_button.label = "Next page"
        self.back_button.label = "Back to list"

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True

    def build_options(self) -> list[discord.SelectOption]:
        start = self.page * self.page_size
        end = min(len(self.reasons), start + self.page_size)
        options: list[discord.SelectOption] = []
        for idx in range(start, end):
            reason = self.reasons[idx]
            label = _truncate_select_label(f"{reason.key} — {reason.title}", 100)
            options.append(discord.SelectOption(label=label, value=str(idx)))
        if not options:
            options.append(discord.SelectOption(label="(no reasons available)", value="-1"))
        return options

    def _source_label(self) -> str:
        if self.reason_set.source == "toolbox_wiki":
            return "Toolbox wiki"
        if self.reason_set.source == "subreddit_rules":
            return "Subreddit rules"
        return "None"

    def build_embed(self) -> discord.Embed:
        if self.reason_set.source == "none" or not self.reasons:
            embed = discord.Embed(
                title="Removal reasons unavailable",
                description=(
                    "No removal reasons could be loaded from the toolbox wiki or subreddit rules.\n"
                    "Use the existing Reply / Removal Message actions with manual text."
                ),
                color=discord.Color.orange(),
            )
            return embed

        if self.selected_index is None:
            page_count = max(1, (len(self.reasons) + self.page_size - 1) // self.page_size)
            embed = discord.Embed(
                title="Select a removal reason",
                description=f"Source: {self._source_label()} • Page {self.page + 1}/{page_count}",
                color=discord.Color.blurple(),
            )
            return embed

        reason = self.reasons[self.selected_index]
        payload = self.report_view.payload
        url = payload.permalink
        title = payload.title or payload.fullname
        if payload.kind == "submission":
            message = render_removal_message(
                self.reason_set,
                reason,
                kind=payload.kind,
                subreddit_name=payload.subreddit,
                title=title,
                url=url,
            )
        else:
            if self.reason_set.source == "subreddit_rules":
                message = render_removal_message(
                    self.reason_set,
                    reason,
                    kind=payload.kind,
                    subreddit_name=payload.subreddit,
                    title=title,
                    url=url,
                )
            else:
                message = reason.text

        preview = message.strip()
        if len(preview) > 3800:
            preview = preview[:3797] + "..."
        embed = discord.Embed(
            title=_truncate_select_label(f"{reason.key} — {reason.title}", 256),
            description=preview or "(empty reason text)",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Source: {self._source_label()}")
        return embed

    async def select_index(self, interaction: discord.Interaction, idx: int) -> None:
        if idx < 0 or idx >= len(self.reasons):
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return
        view = RemovalReasonPickerView(
            report_view=self.report_view,
            message_ref=self.message_ref,
            reason_set=self.reason_set,
            reasons=self.reasons,
            selected_index=idx,
            page=self.page,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Prev page", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.report_view._ensure_mod(interaction):
            return
        if self.page <= 0:
            await interaction.response.defer(ephemeral=True, thinking=False)
            return
        view = RemovalReasonPickerView(
            report_view=self.report_view,
            message_ref=self.message_ref,
            reason_set=self.reason_set,
            reasons=self.reasons,
            selected_index=self.selected_index,
            page=self.page - 1,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Next page", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.report_view._ensure_mod(interaction):
            return
        if (self.page + 1) * self.page_size >= len(self.reasons):
            await interaction.response.defer(ephemeral=True, thinking=False)
            return
        view = RemovalReasonPickerView(
            report_view=self.report_view,
            message_ref=self.message_ref,
            reason_set=self.reason_set,
            reasons=self.reasons,
            selected_index=self.selected_index,
            page=self.page + 1,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Back to list", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.report_view._ensure_mod(interaction):
            return
        view = RemovalReasonPickerView(
            report_view=self.report_view,
            message_ref=self.message_ref,
            reason_set=self.reason_set,
            reasons=self.reasons,
            selected_index=None,
            page=self.page,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success)
    async def open_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.report_view._ensure_mod(interaction):
            return
        if self.selected_index is None:
            await interaction.response.send_message("Select a reason first.", ephemeral=True)
            return
        reason = self.reasons[self.selected_index]
        payload = self.report_view.payload
        url = payload.permalink
        title = payload.title or payload.fullname
        if payload.kind == "submission":
            body = render_removal_message(
                self.reason_set,
                reason,
                kind=payload.kind,
                subreddit_name=payload.subreddit,
                title=title,
                url=url,
            )
            modal = RemovalMessageModal(
                self.report_view,
                self.message_ref,
                default_title=f"Rule {reason.key}",
                default_body=body,
            )
        else:
            body = reason.text
            if self.reason_set.source == "subreddit_rules":
                body = render_removal_message(
                    self.reason_set,
                    reason,
                    kind=payload.kind,
                    subreddit_name=payload.subreddit,
                    title=title,
                    url=url,
                )
            modal = ReplyModal(
                self.report_view,
                self.message_ref,
                default_body=body,
            )
        await interaction.response.send_modal(modal)

        # Best-effort cleanup: disable the picker UI once the modal is opened to avoid
        # leaving interactive controls behind in the ephemeral message.
        try:
            if interaction.message is not None:
                self._disable_all()
                embed = self.build_embed()
                embed.set_footer(text="Modal opened. Submit it to send.")
                await interaction.message.edit(embed=embed, view=self)
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._view.ensure_mod_from_modal(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        body = str(self.body.value or "").strip()
        if not body:
            await interaction.followup.send("Reply body is required.", ephemeral=True)
            return

        remove_raw = str(self.remove_first.value or "y").strip().lower()
        sticky_raw = str(self.sticky.value or "y").strip().lower()
        lock_raw = str(self.lock.value or "n").strip().lower()

        remove_first = remove_raw in {"y", "1", "t"}
        sticky = sticky_raw in {"y", "1", "t"}
        lock = lock_raw in {"y", "1", "t"}

        if self._view.payload.kind != "submission":
            sticky = False

        action_start = time.monotonic()
        try:
            if remove_first:
                await self._view.reddit.remove_item(self._view.payload.fullname, spam=False)
            reply_url = await self._view.reddit.reply(
                fullname=self._view.payload.fullname,
                body=body,
                sticky=sticky,
                lock=lock,
            )
            if remove_first:
                await self._view.reddit.set_ignore_reports(self._view.payload.fullname, True)
        except Exception as exc:
            logger.exception("Reply action failed")
            await interaction.followup.send(f"Reply failed: {exc}", ephemeral=True)
            return
        action_s = time.monotonic() - action_start

        sticky_label = "sticky" if sticky else "no-sticky"
        lock_label = " + locked" if lock else ""
        remove_label = "removed + replied" if remove_first else "replied"
        if reply_url:
            detail = f"[reply]({reply_url}), {sticky_label}{lock_label}"
        else:
            detail = f"{sticky_label}{lock_label}"
        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            f"{remove_label} ({detail})",
            action_duration_s=action_s,
        )


class MoreActionsSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Reply", value="reply"),
            discord.SelectOption(label="Removal reason…", value="removal_reason"),
            discord.SelectOption(label="Modmail", value="modmail"),
            discord.SelectOption(label="Ban user", value="ban"),
            discord.SelectOption(label="Refresh state", value="refresh"),
        ]
        super().__init__(
            placeholder="More actions...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="rmd_more_actions",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ReportView):
            await interaction.response.send_message("Unexpected view.", ephemeral=True)
            return
        if view.payload.handled:
            await interaction.response.send_message("Already marked handled.", ephemeral=True)
            return
        if not await view._ensure_mod(interaction):
            return
        ref = view._message_ref_from_interaction(interaction)
        if ref is None:
            await interaction.response.send_message("Message context unavailable.", ephemeral=True)
            return

        selected = self.values[0]
        if selected == "ban":
            await interaction.response.send_modal(BanModal(view, ref, view.payload.author))
            return
        if selected == "modmail":
            await interaction.response.send_modal(ModmailModal(view, ref, view.payload.author))
            return
        if selected == "reply":
            await interaction.response.send_modal(ReplyModal(view, ref))
            return
        if selected == "removal_reason":
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                reason_set = await view.reddit.fetch_removal_reasons(
                    view.payload.subreddit,
                    kind=view.payload.kind,
                )
            except Exception:
                logger.exception("Failed to load removal reasons")
                await interaction.edit_original_response(
                    content="Failed to load removal reasons. "
                            "Try again or use Reply / Removal Message.",
                )
                return
            reasons = reason_set.applicable_reasons(view.payload.kind)
            picker = RemovalReasonPickerView(
                report_view=view,
                message_ref=ref,
                reason_set=reason_set,
                reasons=reasons,
            )
            await interaction.edit_original_response(
                embed=picker.build_embed(), view=picker
            )
            return
        if selected == "refresh":
            await interaction.response.defer(ephemeral=True, thinking=True)
            total_start = time.monotonic()
            refresh_start = time.monotonic()
            refresh_failed = False
            try:
                await view._refresh_state()
            except Exception as exc:
                refresh_failed = True
                refresh_s = time.monotonic() - refresh_start
                total_s = time.monotonic() - total_start
                view._log_action_timing(
                    interaction,
                    "refreshed state",
                    total_s=total_s,
                    refresh_s=refresh_s,
                    refresh_failed=refresh_failed,
                )
                logger.exception("Refresh state failed")
                await interaction.followup.send(f"Refresh failed: {exc}", ephemeral=True)
                return
            refresh_s = time.monotonic() - refresh_start
            update_start = time.monotonic()
            await view._apply_message_update(interaction, ref)
            update_s = time.monotonic() - update_start
            total_s = time.monotonic() - total_start
            view._log_action_timing(
                interaction,
                "refreshed state",
                total_s=total_s,
                refresh_s=refresh_s,
                update_s=update_s,
            )
            await interaction.followup.send("Refreshed.", ephemeral=True)
            return

        await interaction.response.send_message("Unknown selection.", ephemeral=True)


class ReportView(discord.ui.View):
    def __init__(
        self,
        payload: ReportViewPayload,
        store: BotStore,
        reddit: RedditApi,
        allowed_role_ids: set[int],
        *,
        demo_mode: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.payload = payload
        self.store = store
        self.reddit = reddit
        self.allowed_role_ids = allowed_role_ids
        self.demo_mode = demo_mode
        # The rows the buttons were declared with, so the working card can be
        # put back exactly as it was after somebody reopens the alert.
        self._declared_rows = [(item, item.row) for item in self.children]
        self._open_button = discord.ui.Button(
            label="Open on Reddit",
            style=discord.ButtonStyle.link,
            url=_context_permalink(payload.permalink, payload.kind),
            row=2,
        )
        if payload.handled:
            self._collapse_to_handled()
        else:
            self._restore_actions()

    def _update_toggle_labels(self) -> None:
        self.lock_button.label = "Unlock" if self.payload.locked else "Lock"

    def _collapse_to_handled(self) -> None:
        """A closed alert needs two things: a way to look at the item, and a way
        back if the wrong one got closed.

        Everything else goes, rather than being greyed out. Disabled controls
        left two rows of dead buttons under every card in the queue.
        """
        for child in list(self.children):
            self.remove_item(child)
        self._open_button.row = 0
        self.add_item(self._open_button)
        self.unhandle_button.row = 0
        self.unhandle_button.disabled = False
        self.add_item(self.unhandle_button)

    def _restore_actions(self) -> None:
        """The working card: every action on its declared row, the menu, the link."""
        for child in list(self.children):
            self.remove_item(child)
        for item, row in self._declared_rows:
            if item is self.unhandle_button:
                continue
            item.row = row
            item.disabled = False
            self.add_item(item)
        self._open_button.row = 2
        self.add_item(self._open_button)
        more = MoreActionsSelect()
        more.row = 1
        self.add_item(more)
        self._update_toggle_labels()

    async def _ensure_mod(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if is_allowed_moderator(member, self.allowed_role_ids):
            return True
        await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
        return False

    async def ensure_mod_from_modal(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if is_allowed_moderator(member, self.allowed_role_ids):
            return True
        await interaction.response.send_message("Allowed mod role required.", ephemeral=True)
        return False

    def _message_ref_from_interaction(self, interaction: discord.Interaction) -> MessageRef | None:
        if not interaction.message or not interaction.guild:
            return None
        return MessageRef(
            message_id=interaction.message.id,
            channel_id=interaction.message.channel.id,
            guild_id=interaction.guild.id,
        )

    async def _persist(self, ref: MessageRef) -> None:
        await self.store.save_view(
            ViewRecord(
                message_id=ref.message_id,
                channel_id=ref.channel_id,
                guild_id=ref.guild_id,
                payload=self.payload.to_dict(),
                created_at=time.time(),
            )
        )

    async def _fetch_message_for_ref(
        self,
        interaction: discord.Interaction,
        ref: MessageRef,
    ) -> discord.Message | None:
        client = interaction.client
        channel = client.get_channel(ref.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            guild = interaction.guild
            if guild is None:
                guild_obj = client.get_guild(ref.guild_id)
            else:
                guild_obj = guild
            if guild_obj is not None:
                try:
                    fetched = await guild_obj.fetch_channel(ref.channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        try:
            return await channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _append_action(self, interaction: discord.Interaction, action_text: str) -> None:
        user = interaction.user
        actor = user.display_name if isinstance(user, discord.Member) else str(user)
        self.payload.action_log.append(f"{_utc_stamp()} - {actor}: {action_text}")
        if self.demo_mode:
            logger.info("[demo] %s %s", self.payload.fullname, action_text)

    def _log_action_timing(
        self,
        interaction: discord.Interaction,
        action_text: str,
        *,
        total_s: float,
        action_s: float | None = None,
        refresh_s: float | None = None,
        update_s: float | None = None,
        refresh_failed: bool = False,
    ) -> None:
        user = interaction.user
        actor = user.display_name if isinstance(user, discord.Member) else str(user)
        stamp = _utc_stamp()
        parts = [f"total={_format_duration(total_s)}"]
        if action_s is not None:
            parts.append(f"action={_format_duration(action_s)}")
        if refresh_s is not None:
            parts.append(f"refresh={_format_duration(refresh_s)}")
        if update_s is not None:
            parts.append(f"update={_format_duration(update_s)}")
        if refresh_failed:
            parts.append("refresh_failed")
        logger.info(
            "Audit Log %s - %s: %s (%s) [r/%s %s]",
            stamp,
            actor,
            action_text,
            ", ".join(parts),
            self.payload.subreddit,
            self.payload.fullname,
        )

    async def _refresh_state(self) -> None:
        state = await self.reddit.refresh_state(self.payload.fullname)
        self.payload.locked = bool(state.get("locked", self.payload.locked))
        self.payload.reports_ignored = bool(
            state.get("reports_ignored", self.payload.reports_ignored)
        )
        self.payload.removed = bool(state.get("removed", self.payload.removed))
        self.payload.approved = bool(state.get("approved", self.payload.approved))
        raw_num_reports = state.get("num_reports", self.payload.num_reports)
        if isinstance(raw_num_reports, (int, float, str)):
            try:
                self.payload.num_reports = int(raw_num_reports)
            except ValueError:
                pass
        raw_num_comments = state.get("num_comments", self.payload.num_comments)
        if isinstance(raw_num_comments, (int, float, str)):
            try:
                self.payload.num_comments = int(raw_num_comments)
            except ValueError:
                pass
        self._update_toggle_labels()

    async def _pull_reddit_side_actions(
        self, interaction: discord.Interaction, ref: MessageRef
    ) -> None:
        """Fold in anything done on Reddit before the moderator pressed the button."""
        setup_id = self.payload.setup_id or str(ref.guild_id)
        subreddit = self.payload.subreddit
        entries: list = []
        if subreddit:
            try:
                entries = await self.reddit.fetch_recent_modlog_entries(
                    subreddit, limit=_HANDLED_MODLOG_LIMIT
                )
            except Exception:
                logger.exception("Failed to fetch modlog after Mark Handled")
        if entries:
            try:
                await self.store.save_modlog_entries(setup_id, entries)
            except Exception:
                logger.exception("Failed to persist modlog entries")

        changed = False
        existing = set(self.payload.action_log)
        for fullname, _created_utc, line in entries:
            if fullname != self.payload.fullname or line in existing:
                continue
            self.payload.action_log.append(line)
            existing.add(line)
            changed = True

        # Also refresh removed/approved so the Status line reflects the action
        # rather than only saying handled.
        try:
            before = (self.payload.removed, self.payload.approved, self.payload.locked)
            await self._refresh_state()
            if (self.payload.removed, self.payload.approved, self.payload.locked) != before:
                changed = True
        except Exception:
            logger.exception("Failed to refresh state after Mark Handled")

        if changed:
            await self._apply_message_update(interaction, ref)

    async def _apply_message_update(self, interaction: discord.Interaction, ref: MessageRef) -> None:
        msg = await self._fetch_message_for_ref(interaction, ref)
        if msg is not None:
            embed, attachment = build_report_message(self.payload)
            edit_kwargs: dict[str, object] = {"embed": embed, "view": self}
            if attachment is not None:
                edit_kwargs["attachments"] = [attachment]
            try:
                await msg.edit(**edit_kwargs)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception("Failed to edit alert message %s", ref.message_id)
        try:
            await self._persist(ref)
        except Exception:
            logger.exception("Failed to persist alert payload for message %s", ref.message_id)
        if self.payload.handled:
            setup_id = self.payload.setup_id or str(ref.guild_id)
            try:
                await self.store.mark_handled(self.payload.fullname, setup_id)
            except Exception:
                logger.exception("Failed to mark item handled: %s", self.payload.fullname)

    async def complete_modal_action(
        self,
        interaction: discord.Interaction,
        ref: MessageRef,
        action_text: str,
        *,
        action_duration_s: float | None = None,
    ) -> None:
        total_start = time.monotonic()
        self._append_action(interaction, action_text)
        refresh_start = time.monotonic()
        refresh_failed = False
        try:
            await self._refresh_state()
        except Exception:
            refresh_failed = True
            logger.exception("Failed to refresh Reddit state after modal action")
        refresh_s = time.monotonic() - refresh_start
        update_start = time.monotonic()
        await self._apply_message_update(interaction, ref)
        update_s = time.monotonic() - update_start
        total_s = time.monotonic() - total_start
        if action_duration_s is not None:
            total_s += action_duration_s
        self._log_action_timing(
            interaction,
            action_text,
            total_s=total_s,
            action_s=action_duration_s,
            refresh_s=refresh_s,
            update_s=update_s,
            refresh_failed=refresh_failed,
        )
        await interaction.followup.send(f"Done: {action_text}", ephemeral=True)

    async def _notify_failure(self, interaction: discord.Interaction, content: str) -> None:
        """Tell the moderator that something did not work.

        Only used when an action fails. Success is visible on the card itself,
        so saying so again costs the moderator a message to dismiss.

        This must stay a followup. These buttons acknowledge with a deferred
        update, so the interaction's original response is the alert message, and
        editing it would replace the card with this text.
        """
        try:
            await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException:
            logger.debug("Could not deliver failure notice to moderator: %s", content)

    def _mark_action_failed(self, action_text: str) -> None:
        """Flip an optimistic action log line to show the action did not take."""
        for i in range(len(self.payload.action_log) - 1, -1, -1):
            if self.payload.action_log[i].endswith(action_text):
                self.payload.action_log[i] += "  (FAILED)"
                return

    async def _finish_button_action(
        self,
        interaction: discord.Interaction,
        ref: "MessageRef",
        action_text: str,
        action_coro,
        *,
        mark_reviewed: bool,
        total_start: float,
    ) -> None:
        """Do the slow Reddit half after the moderator has already been answered."""
        action_start = time.monotonic()
        try:
            await action_coro()
            if mark_reviewed:
                await self.reddit.set_ignore_reports(self.payload.fullname, True)
        except Exception as exc:
            logger.exception("Action failed: %s", action_text)
            self._mark_action_failed(action_text)
            try:
                await self._apply_message_update(interaction, ref)
            except Exception:
                logger.exception("Failed to correct alert after a failed action")
            await self._notify_failure(interaction, f"Action failed: {exc}")
            return
        action_s = time.monotonic() - action_start

        refresh_start = time.monotonic()
        refresh_failed = False
        try:
            await self._refresh_state()
        except Exception:
            refresh_failed = True
            logger.exception("Failed to refresh Reddit state after action")
        refresh_s = time.monotonic() - refresh_start

        update_start = time.monotonic()
        try:
            await self._apply_message_update(interaction, ref)
        except Exception:
            logger.exception("Failed to apply post action update")
        update_s = time.monotonic() - update_start

        self._log_action_timing(
            interaction,
            action_text,
            total_s=time.monotonic() - total_start,
            action_s=action_s,
            refresh_s=refresh_s,
            update_s=update_s,
            refresh_failed=refresh_failed,
        )

    async def _run_button_action(
        self,
        interaction: discord.Interaction,
        action_text: str,
        action_coro,
        *,
        mark_reviewed: bool = False,
    ) -> None:
        if self.payload.handled:
            await interaction.response.send_message("Already marked handled.", ephemeral=True)
            return
        if not await self._ensure_mod(interaction):
            return
        ref = self._message_ref_from_interaction(interaction)
        if ref is None:
            await interaction.response.send_message("Message context unavailable.", ephemeral=True)
            return

        # Apply optimistically. The Reddit write is the slow part, p90 about 15s
        # and worst case 36s, and it accounted for 71% of button latency while
        # the moderator sat watching a spinner. Record the action and redraw the
        # alert straight away; the write runs in the background and corrects the
        # alert if it fails.
        #
        # A deferred update rather than a thinking reply: it acknowledges the
        # press without creating a message, so the redrawn card is the feedback.
        await interaction.response.defer()
        total_start = time.monotonic()
        self._append_action(interaction, action_text)
        try:
            await self._apply_message_update(interaction, ref)
        except Exception:
            logger.exception("Failed to update alert optimistically")

        # Held rather than fired and forgotten. A failure in the background half
        # must reach the log, otherwise the moderator is left with an alert that
        # says the action was applied when it was not.
        _spawn(
            self._finish_button_action(
                interaction,
                ref,
                action_text,
                action_coro,
                mark_reviewed=mark_reviewed,
                total_start=total_start,
            ),
            "button action %r on %s" % (action_text, self.payload.fullname),
        )

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="rmd_approve",
        row=0,
    )
    async def approve_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        async def approve_and_ignore() -> None:
            await self.reddit.approve_item(self.payload.fullname)
            await self.reddit.set_ignore_reports(self.payload.fullname, True)

        await self._run_button_action(interaction, "approved + ignored reports", approve_and_ignore)

    @discord.ui.button(
        label="Remove",
        style=discord.ButtonStyle.danger,
        custom_id="rmd_remove",
        row=0,
    )
    async def remove_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_button_action(
            interaction,
            "removed item",
            lambda: self.reddit.remove_item(self.payload.fullname, spam=False),
            mark_reviewed=True,
        )

    @discord.ui.button(
        label="Spam",
        style=discord.ButtonStyle.danger,
        custom_id="rmd_spam",
        row=0,
    )
    async def spam_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._run_button_action(
            interaction,
            "removed as spam",
            lambda: self.reddit.remove_item(self.payload.fullname, spam=True),
            mark_reviewed=True,
        )

    @discord.ui.button(
        label="Lock",
        style=discord.ButtonStyle.danger,
        custom_id="rmd_lock_toggle",
        row=0,
    )
    async def lock_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        new_locked_state = not self.payload.locked
        verb = "locked" if new_locked_state else "unlocked"
        await self._run_button_action(
            interaction,
            f"{verb} item",
            lambda: self.reddit.set_lock(self.payload.fullname, new_locked_state),
        )

    @discord.ui.button(
        label="Mark Unhandled",
        style=discord.ButtonStyle.secondary,
        custom_id="rmd_mark_unhandled",
        row=0,
    )
    async def unhandle_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Put a closed alert back in the queue.

        Mark Handled used to be a one way door, so a mis-press meant the alert
        was gone with nothing to press to get it back.
        """
        if not await self._ensure_mod(interaction):
            return
        if not self.payload.handled:
            await interaction.response.send_message("This one is already open.", ephemeral=True)
            return
        ref = self._message_ref_from_interaction(interaction)
        if ref is None:
            await interaction.response.send_message("Message context unavailable.", ephemeral=True)
            return

        if self.demo_mode:
            self._append_action(interaction, "marked unhandled (demo)")
            await interaction.response.defer()
            await self._apply_message_update(interaction, ref)
            return

        self.payload.handled = False
        self._append_action(interaction, "marked unhandled")
        self._restore_actions()
        start = time.monotonic()
        await interaction.response.defer()
        await self._apply_message_update(interaction, ref)
        setup_id = self.payload.setup_id or str(ref.guild_id)
        try:
            await self.store.mark_unhandled(self.payload.fullname, setup_id)
        except Exception:
            logger.exception("Failed to reopen item: %s", self.payload.fullname)
        total_s = time.monotonic() - start
        self._log_action_timing(
            interaction,
            "marked unhandled",
            total_s=total_s,
            update_s=total_s,
        )

    @discord.ui.button(
        label="Mark Handled",
        style=discord.ButtonStyle.secondary,
        custom_id="rmd_mark_handled",
        row=2,
    )
    async def handled_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        if self.payload.handled:
            await interaction.response.send_message("Already marked handled.", ephemeral=True)
            return
        ref = self._message_ref_from_interaction(interaction)
        if ref is None:
            await interaction.response.send_message("Message context unavailable.", ephemeral=True)
            return

        if self.demo_mode:
            self._append_action(interaction, "marked handled (demo)")
            start = time.monotonic()
            await interaction.response.defer(ephemeral=True, thinking=False)
            await self._apply_message_update(interaction, ref)
            total_s = time.monotonic() - start
            self._log_action_timing(
                interaction,
                "marked handled (demo)",
                total_s=total_s,
                update_s=total_s,
            )
            await interaction.followup.send("Logged (demo).", ephemeral=True)
            return

        self.payload.handled = True
        self._append_action(interaction, "marked handled")
        self._collapse_to_handled()
        start = time.monotonic()
        await interaction.response.defer()
        await self._apply_message_update(interaction, ref)
        total_s = time.monotonic() - start
        self._log_action_timing(
            interaction,
            "marked handled",
            total_s=total_s,
            update_s=total_s,
        )

        # Done in the background so the button stays instant: the card is
        # already redrawn and updates again a moment later if Reddit shows the
        # moderator acted there.
        _spawn(self._pull_reddit_side_actions(interaction, ref),
               "Reddit side actions for %s" % self.payload.fullname)
