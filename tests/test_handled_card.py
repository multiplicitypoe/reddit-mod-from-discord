"""A closed alert should read as a record, and closing it should be reversible.

Marking an alert handled left every button in place, greyed out: two rows of
dead controls under a card nobody can act on. It was also a one way door, so a
mis-press meant the alert was gone from the queue with no way back.

A handled card now carries two things. A way to look at the item, and a way to
undo the close.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import discord  # noqa: E402

from reddit_mod_from_discord.discord_ui.report_view import ReportView  # noqa: E402
from reddit_mod_from_discord.models import ReportViewPayload  # noqa: E402


def payload(**kw):
    base = dict(
        fullname="t3_abc",
        kind="submission",
        subreddit="pathofexile",
        author="someone",
        permalink="https://reddit.com/r/pathofexile/comments/abc",
        link_url=None,
        media_url=None,
        thumbnail_url=None,
        title="A post",
        snippet="body",
        num_reports=3,
        created_utc=0.0,
        num_comments=0,
        locked=False,
        reports_ignored=False,
        removed=False,
        approved=False,
        user_reports=[],
        mod_reports=[],
    )
    base.update(kw)
    return ReportViewPayload(**base)


def view_for(**kw):
    # discord.py wants a running loop to build a View, so give it one.
    async def build():
        return ReportView(payload(**kw), store=None, reddit=None, allowed_role_ids=set())

    return asyncio.run(build())


def labels(view):
    return [getattr(c, "label", None) or type(c).__name__ for c in view.children]


def test_an_open_alert_keeps_its_full_toolset():
    shown = labels(view_for())
    assert "Approve" in shown
    assert "Remove" in shown
    assert "Mark Handled" in shown
    assert "Mark Unhandled" not in shown


def test_a_handled_alert_shows_only_a_link_and_a_way_back():
    view = view_for(handled=True)
    shown = labels(view)
    assert shown == ["Open on Reddit", "Mark Unhandled"], shown


def test_a_handled_alert_fits_on_one_row():
    view = view_for(handled=True)
    rows = {getattr(c, "row", None) for c in view.children}
    assert rows == {0}, f"handled card spread over rows {sorted(rows)}"


def test_nothing_on_a_handled_card_is_a_dead_control():
    for child in view_for(handled=True).children:
        assert not getattr(child, "disabled", False), f"{child} is disabled rather than gone"


def test_unmarking_restores_the_toolset():
    """The reverse of the collapse, in place, without rebuilding the message."""
    view = view_for(handled=True)
    view.payload.handled = False
    view._restore_actions()
    shown = labels(view)
    assert "Approve" in shown
    assert "Mark Handled" in shown
    assert "Mark Unhandled" not in shown
    assert "Open on Reddit" in shown


def test_collapsing_and_restoring_repeatedly_does_not_stack_duplicates():
    view = view_for()
    for _ in range(3):
        view.payload.handled = True
        view._collapse_to_handled()
        view.payload.handled = False
        view._restore_actions()
    shown = labels(view)
    assert len(shown) == len(set(shown)), f"duplicated controls: {shown}"
