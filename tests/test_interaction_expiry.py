"""A button press must not be silently dropped just because Discord expired
the interaction token before the bot got to acknowledge it.

Discord invalidates an interaction 3 seconds after creating it. A gateway
hiccup can eat that whole budget before the handler even runs, so
interaction.response.defer() raises NotFound (error code 10062, "Unknown
interaction"). Nothing before defer() catches it, so the exception aborted
_run_button_action entirely: the Reddit removal, the audit-log append, and the
card update never ran. The mod's click vanished with no trace beyond
Discord's generic "The application didn't respond in time" and a swallowed
traceback in discord.py's own view dispatcher.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import discord  # noqa: E402

from reddit_mod_from_discord.discord_ui.report_view import ReportView  # noqa: E402


class _FakeResponse:
    status = 404
    reason = "Not Found"


class _ExpiredDeferResponse:
    async def defer(self, *args, **kwargs):
        raise discord.NotFound(_FakeResponse(), "Unknown interaction")


class _Interaction:
    def __init__(self):
        self.response = _ExpiredDeferResponse()


def test_safe_defer_swallows_an_already_expired_interaction():
    """The one thing this must never do is let the NotFound propagate."""
    interaction = _Interaction()
    asyncio.run(ReportView._safe_defer(object(), interaction))
