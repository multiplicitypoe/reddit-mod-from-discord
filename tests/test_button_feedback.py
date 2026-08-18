"""Button presses report through the alert card, not through ephemerals.

Each press is its own interaction, so every press that answers with a visible
ephemeral leaves the moderator another "Only you can see this" message to
dismiss. Removing an item and then marking it handled left two. The card already
shows the audit log, so on success nothing needs to be said.

The failure notice must never reach for edit_original_response. These buttons
acknowledge with a silent deferred update, which makes the "original response"
the alert message itself, so editing it would blank the card.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.discord_ui.report_view import ReportView  # noqa: E402


class _Followup:
    def __init__(self, calls):
        self._calls = calls

    async def send(self, content, ephemeral=False):
        self._calls.append(("followup", content, ephemeral))


class _Interaction:
    def __init__(self, calls):
        self.followup = _Followup(calls)
        self._calls = calls

    async def edit_original_response(self, **kwargs):
        self._calls.append(("edit_original_response", kwargs))


def test_failure_notice_sends_one_ephemeral_and_never_edits_the_alert():
    calls = []
    interaction = _Interaction(calls)
    asyncio.run(
        ReportView._notify_failure(object(), interaction, "Action failed: boom")
    )
    assert calls == [("followup", "Action failed: boom", True)]


def test_failure_notice_survives_discord_refusing_the_send():
    """A moderator losing the notice must not take the background task down."""
    import discord

    class _FakeResponse:
        status = 503
        reason = "Service Unavailable"

    class _Broken(_Interaction):
        async def _boom(self, *a, **k):
            raise discord.HTTPException(_FakeResponse(), "nope")

    interaction = _Broken([])
    interaction.followup.send = interaction._boom
    asyncio.run(ReportView._notify_failure(object(), interaction, "Action failed"))
