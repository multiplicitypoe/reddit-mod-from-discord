"""Display rules for audit log lines that come from the subreddit modlog.

A modlog line arrives as "<action> [modlog] (<detail>)". The action name is
Reddit's internal one and needs mapping to something a moderator reads, while
the detail is sometimes the most useful part of the line (which automod rule
fired, which removal reason was applied) and sometimes pure noise that repeats
the action back ("remove", "unspam") or is an internal id.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.discord_ui.report_view import (  # noqa: E402
    _normalize_audit_log_entry,
)


def body(line):
    """Everything after the timestamp, which is what these rules govern."""
    return _normalize_audit_log_entry(line).split(" - ", 1)[1]


def test_maps_action_and_drops_detail_that_just_repeats_it():
    line = "2026-08-17 18:32 UTC - u/Modaline: removecomment [modlog] (remove)"
    assert body(line) == "u/Modaline: removed"


def test_keeps_detail_naming_the_rule_that_fired():
    line = "2026-08-17 18:32 UTC - u/AutoModerator: removecomment [modlog] (Reputation Filter)"
    assert body(line) == "u/AutoModerator: removed (Reputation Filter)"


def test_drops_internal_id_detail_and_maps_removal_reason():
    line = "2026-08-17 18:33 UTC - u/Modaline: addremovalreason [modlog] (2)"
    assert body(line) == "u/Modaline: added removal reason"


def test_drops_unspam_noise_from_approvals():
    line = "2026-08-17 18:33 UTC - u/Modaline: approvecomment [modlog] (unspam)"
    assert body(line) == "u/Modaline: approved"


def test_no_stray_whitespace_when_there_is_no_detail():
    line = "2026-08-17 18:33 UTC - u/Modaline: removecomment [modlog]"
    assert body(line) == "u/Modaline: removed"


def test_maps_the_remaining_actions_seen_in_this_modlog():
    cases = {
        "sticky": "stickied",
        "unsticky": "unstickied",
        "distinguish": "distinguished",
        "spoiler": "marked spoiler",
        "unspoiler": "unmarked spoiler",
        "marknsfw": "marked NSFW",
        "editflair": "edited flair",
        "setsuggestedsort": "set suggested sort",
        "snoozereports": "snoozed reports",
    }
    for action, expected in cases.items():
        line = "2026-08-17 18:33 UTC - u/Modaline: %s [modlog]" % action
        assert body(line) == "u/Modaline: %s" % expected, action


def test_the_bots_own_wording_is_not_run_through_the_action_mapping():
    """The mapping is for Reddit's action names, not for lines the bot wrote."""
    out = _normalize_audit_log_entry("11:33 PDT - Moda: marked handled")
    assert out.endswith(" - Moda: marked handled")
