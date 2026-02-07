from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from reddit_mod_from_discord.models import ReportViewPayload
from reddit_mod_from_discord.permissions import is_allowed_moderator
from reddit_mod_from_discord.reddit_client import RedditService
from reddit_mod_from_discord.store import BotStore, ViewRecord

logger = logging.getLogger("reddit_mod_from_discord")


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _format_timestamp(ts: float) -> str:
    if ts <= 0:
        return "unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def build_report_embed(payload: ReportViewPayload) -> discord.Embed:
    thing_label = "Post" if payload.kind == "submission" else "Comment"
    if payload.handled:
        color = discord.Color.green()
    elif payload.removed:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()
    title = f"Reported {thing_label} in r/{payload.subreddit}"
    embed = discord.Embed(title=_truncate(title, 256), color=color, url=payload.permalink)
    summary = payload.title if payload.title else thing_label
    description = f"**{_truncate(summary, 300)}**"
    if payload.snippet:
        description += f"\n{_truncate(payload.snippet, 900)}"
    embed.description = description

    if payload.media_url:
        embed.set_image(url=payload.media_url)
    elif payload.thumbnail_url:
        embed.set_thumbnail(url=payload.thumbnail_url)

    status: list[str] = []
    if payload.approved:
        status.append("approved")
    if payload.removed:
        status.append("removed")
    if payload.locked:
        status.append("locked")
    if payload.reports_ignored:
        status.append("reports ignored")
    if payload.handled:
        status.append("handled")

    embed.add_field(name="Author", value=payload.author or "[deleted]", inline=True)
    embed.add_field(name="Reports", value=str(payload.num_reports), inline=True)
    if status:
        status_value = ", ".join(status)
    else:
        status_value = "active (not approved/removed)"
    embed.add_field(name="Status", value=status_value, inline=True)
    if (
        payload.link_url
        and payload.link_url != payload.permalink
        and payload.link_url != payload.media_url
    ):
        embed.add_field(name="Link", value=_truncate(payload.link_url, 1024), inline=False)
    if payload.user_reports or payload.mod_reports:
        embed.add_field(
            name="Report Details",
            value="Use `More actions...` -> `View reports`",
            inline=False,
        )
    if payload.action_log:
        embed.add_field(
            name="Actions",
            value=_truncate("\n".join(f"- {line}" for line in payload.action_log[-10:]), 1024),
            inline=False,
        )
    embed.set_footer(text=f"{payload.fullname} | Created {_format_timestamp(payload.created_utc)}")
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
        label="Ban reason",
        required=False,
        max_length=100,
    )
    mod_note = discord.ui.TextInput(
        label="Mod note",
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

        try:
            await self._view.reddit.ban_user(
                subreddit_name=self._view.payload.subreddit,
                username=username,
                duration_days=duration_days,
                ban_reason=str(self.ban_reason.value or "").strip(),
                mod_note=str(self.mod_note.value or "").strip(),
                ban_message=str(self.ban_message.value or "").strip(),
            )
        except Exception as exc:
            logger.exception("Ban action failed")
            await interaction.followup.send(f"Ban failed: {exc}", ephemeral=True)
            return

        duration_label = f"{duration_days}d" if duration_days else "permanent"
        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            f"banned u/{username} ({duration_label})",
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

        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            "sent removal message as subreddit",
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

        try:
            await self._view.reddit.send_modmail(
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

        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            f"sent modmail to u/{recipient}",
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

        try:
            if remove_first:
                await self._view.reddit.remove_item(self._view.payload.fullname, spam=False)
            await self._view.reddit.reply(
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

        sticky_label = "sticky" if sticky else "no-sticky"
        lock_label = " + locked" if lock else ""
        remove_label = "removed + replied" if remove_first else "replied"
        await self._view.complete_modal_action(
            interaction,
            self._message_ref,
            f"{remove_label} ({sticky_label}{lock_label})",
        )


class MoreActionsSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="View reports", value="view_reports"),
            discord.SelectOption(label="Toggle ignore reports", value="toggle_ignore"),
            discord.SelectOption(label="Reply", value="reply"),
            discord.SelectOption(label="Modmail (author hidden)", value="modmail"),
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
        if selected == "view_reports":
            lines: list[str] = []
            if view.payload.user_reports:
                lines.append("User reports:")
                lines.extend([f"- {line}" for line in view.payload.user_reports])
            if view.payload.mod_reports:
                lines.append("Mod reports:")
                lines.extend([f"- {line}" for line in view.payload.mod_reports])
            if not lines:
                lines = ["No report details available on this alert."]
            await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)
            return
        if selected == "toggle_ignore":
            await interaction.response.defer(ephemeral=True, thinking=True)
            new_state = not view.payload.reports_ignored
            verb = "ignored reports" if new_state else "unignored reports"
            try:
                await view.reddit.set_ignore_reports(view.payload.fullname, new_state)
            except Exception as exc:
                logger.exception("Toggle ignore reports failed")
                await interaction.followup.send(f"Toggle failed: {exc}", ephemeral=True)
                return
            view._append_action(interaction, verb)
            try:
                await view._refresh_state()
            except Exception:
                logger.exception("Failed to refresh state after toggle")
            await view._apply_message_update(interaction, ref)
            await interaction.followup.send(f"Done: {verb}", ephemeral=True)
            return
        if selected == "modmail":
            await interaction.response.send_modal(ModmailModal(view, ref, view.payload.author))
            return
        if selected == "reply":
            await interaction.response.send_modal(ReplyModal(view, ref))
            return
        if selected == "refresh":
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await view._refresh_state()
            except Exception as exc:
                logger.exception("Refresh state failed")
                await interaction.followup.send(f"Refresh failed: {exc}", ephemeral=True)
                return
            await view._apply_message_update(interaction, ref)
            await interaction.followup.send("Refreshed.", ephemeral=True)
            return

        await interaction.response.send_message("Unknown selection.", ephemeral=True)


class ReportView(discord.ui.View):
    def __init__(
        self,
        payload: ReportViewPayload,
        store: BotStore,
        reddit: RedditService,
        allowed_role_ids: set[int],
    ) -> None:
        super().__init__(timeout=None)
        self.payload = payload
        self.store = store
        self.reddit = reddit
        self.allowed_role_ids = allowed_role_ids
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
        stamp = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
        self.payload.action_log.append(f"{stamp} - {actor}: {action_text}")

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
            try:
                await self.store.mark_handled(self.payload.fullname)
            except Exception:
                logger.exception("Failed to mark item handled: %s", self.payload.fullname)

    async def complete_modal_action(
        self,
        interaction: discord.Interaction,
        ref: MessageRef,
        action_text: str,
    ) -> None:
        self._append_action(interaction, action_text)
        try:
            await self._refresh_state()
        except Exception:
            logger.exception("Failed to refresh Reddit state after modal action")
        await self._apply_message_update(interaction, ref)
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
        try:
            await action_coro()
            if mark_reviewed:
                await self.reddit.set_ignore_reports(self.payload.fullname, True)
        except Exception as exc:
            logger.exception("Action failed: %s", action_text)
            await interaction.followup.send(f"Action failed: {exc}", ephemeral=True)
            return

        self._append_action(interaction, action_text)
        try:
            await self._refresh_state()
        except Exception:
            logger.exception("Failed to refresh Reddit state after action")
        await self._apply_message_update(interaction, ref)
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

        self.payload.handled = True
        self._append_action(interaction, "marked handled")
        self._disable_actions()
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._apply_message_update(interaction, ref)
        await interaction.followup.send("Marked handled.", ephemeral=True)
