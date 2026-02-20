from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from reddit_mod_from_discord.models import ReportViewPayload
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import RedditApi
from reddit_mod_from_discord.safety import sanitize_http_url
from reddit_mod_from_discord.store import BotStore, ViewRecord

logger = logging.getLogger("reddit_mod_from_discord")

_BAN_REASON_API_MAX = 100
_BAN_NOTE_API_MAX = 300
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


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
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}) (?P<tz>PST|PDT) - (?P<rest>.+)$"
)
_MODLOG_ACTION_RE = re.compile(r"^u/(?P<mod>[^:]+): (?P<action>.+)$")
_CONFIRM_SUFFIX_RE = re.compile(r"\s*\((confirm_ham|confirm_spam)\)\s*$")


def _format_local_hhmm(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_DISPLAY_TZ)
    return dt.strftime("%H:%M %Z")


def _normalize_modlog_action_text(action_text: str) -> str:
    # Strip internal marker and noisy confirm_* details.
    text = str(action_text).replace("[modlog]", "").strip()
    text = _CONFIRM_SUFFIX_RE.sub("", text).strip()
    lowered = text.lower()
    mapping = {
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
    }
    if lowered in mapping:
        return mapping[lowered]
    return text


def _normalize_audit_log_entry(line: str) -> str:
    """
    Normalize stored audit log lines for display:
    - Always render timestamp as HH:MM PST/PDT.
    - Collapse modlog entries into a similar shape as in-bot actions.
    """
    raw = str(line).strip()
    if not raw:
        return raw

    local_match = _LOCAL_STAMP_RE.match(raw)
    if local_match is not None:
        # Already in desired time format.
        return raw

    now_utc = datetime.now(tz=timezone.utc)

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
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        candidates: list[datetime] = []
        for day_offset in (0, -1, 1):
            dt = (now_utc + timedelta(days=day_offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            candidates.append(dt)
        dt_utc = min(candidates, key=lambda dt: abs((now_utc - dt).total_seconds()))
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


def build_report_embed(payload: ReportViewPayload) -> discord.Embed:
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

    subreddit = _escape_discord_text(payload.subreddit)
    author = _escape_discord_text(payload.author or "[deleted]")
    title = f"Reported {thing_label} in /r/{subreddit} by {author}"
    embed = discord.Embed(title=_truncate(title, 256), color=color, url=safe_permalink)
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

    description_lines = [f"**Title:** {_truncate(summary, 300)}"]
    description_lines.append(f"**Status:** {status_value}")
    if payload.kind == "submission" and payload.num_comments is not None:
        description_lines.append(f"**Comments:** {payload.num_comments}")

    if (
        safe_link_url
        and safe_link_url != safe_permalink
        and safe_link_url != safe_media_url
    ):
        description_lines.append(f"**Link:** {safe_link_url}")

    if payload.snippet:
        raw_snippet = payload.snippet.strip()
        safe_snippet = sanitize_http_url(raw_snippet)
        if safe_snippet and safe_snippet in {safe_link_url, safe_permalink, safe_media_url}:
            raw_snippet = ""
        if raw_snippet:
            description_lines.append(
                f"**Text:** {_truncate(_escape_discord_text(raw_snippet), 900)}"
            )

    embed.description = "\n".join(description_lines)

    if safe_media_url:
        embed.set_image(url=safe_media_url)
    elif safe_thumbnail_url:
        embed.set_thumbnail(url=safe_thumbnail_url)

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
        label="Short title (ignored for public comments)",
        required=False,
        max_length=100,
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

    def __init__(self, view: "ReportView", message_ref: MessageRef) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref

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
            "sent removal message as subreddit",
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

    def __init__(self, view: "ReportView", message_ref: MessageRef) -> None:
        super().__init__()
        self._view = view
        self._message_ref = message_ref

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
        self.add_item(
            discord.ui.Button(
                label="Open on Reddit",
                style=discord.ButtonStyle.link,
                url=payload.permalink,
                row=2,
            )
        )
        more = MoreActionsSelect()
        more.row = 1
        self.add_item(more)
        self._update_toggle_labels()
        if payload.handled:
            self._disable_actions()

    def _update_toggle_labels(self) -> None:
        self.lock_button.label = "Unlock" if self.payload.locked else "Lock"

    def _disable_actions(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.style != discord.ButtonStyle.link:
                child.disabled = True
            if isinstance(child, discord.ui.Select):
                child.disabled = True

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
        stamp = datetime.now(tz=_DISPLAY_TZ).strftime("%H:%M %Z")
        self.payload.action_log.append(f"{stamp} - {actor}: {action_text}")
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
        stamp = datetime.now(tz=_DISPLAY_TZ).strftime("%H:%M %Z")
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

    async def _apply_message_update(self, interaction: discord.Interaction, ref: MessageRef) -> None:
        msg = await self._fetch_message_for_ref(interaction, ref)
        if msg is not None:
            try:
                await msg.edit(embed=build_report_embed(self.payload), view=self)
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        total_start = time.monotonic()
        action_start = time.monotonic()
        try:
            await action_coro()
            if mark_reviewed:
                await self.reddit.set_ignore_reports(self.payload.fullname, True)
        except Exception as exc:
            logger.exception("Action failed: %s", action_text)
            await interaction.followup.send(f"Action failed: {exc}", ephemeral=True)
            return
        action_s = time.monotonic() - action_start

        self._append_action(interaction, action_text)
        refresh_start = time.monotonic()
        refresh_failed = False
        try:
            await self._refresh_state()
        except Exception:
            refresh_failed = True
            logger.exception("Failed to refresh Reddit state after action")
        refresh_s = time.monotonic() - refresh_start
        update_start = time.monotonic()
        await self._apply_message_update(interaction, ref)
        update_s = time.monotonic() - update_start
        total_s = time.monotonic() - total_start
        self._log_action_timing(
            interaction,
            action_text,
            total_s=total_s,
            action_s=action_s,
            refresh_s=refresh_s,
            update_s=update_s,
            refresh_failed=refresh_failed,
        )
        await interaction.followup.send(f"Done: {action_text}", ephemeral=True)

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
        self._disable_actions()
        start = time.monotonic()
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._apply_message_update(interaction, ref)
        total_s = time.monotonic() - start
        self._log_action_timing(
            interaction,
            "marked handled",
            total_s=total_s,
            update_s=total_s,
        )
        await interaction.followup.send("Marked handled.", ephemeral=True)
