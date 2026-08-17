"""Audit log timestamps render in the moderators' timezone, Eastern.

Two kinds of line reach the renderer. Modlog lines arrive stamped in UTC with a
full date. The bot's own action lines were written straight to storage already
formatted in local time, so old cards hold Pacific stamps and those need to come
out Eastern too, otherwise a single card mixes two zones.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.discord_ui.report_view import (  # noqa: E402
    _DISPLAY_TZ,
    _normalize_audit_log_entry,
)

# A fixed reference point for the stamps that carry no date.
SUMMER = datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc)


def test_default_display_timezone_is_eastern():
    assert str(_DISPLAY_TZ) == "America/New_York"


def test_utc_modlog_line_renders_in_eastern_daylight_time():
    line = "2026-08-17 18:32 UTC - u/Modaline: removecomment [modlog] (remove)"
    assert _normalize_audit_log_entry(line) == "14:32 EDT - u/Modaline: removed"


def test_utc_modlog_line_renders_in_eastern_standard_time():
    line = "2026-01-15 18:32 UTC - u/Modaline: removecomment [modlog] (remove)"
    assert _normalize_audit_log_entry(line) == "13:32 EST - u/Modaline: removed"


def test_pacific_stamp_stored_on_an_old_card_converts_to_eastern():
    line = "11:33 PDT - Moda: marked handled"
    assert _normalize_audit_log_entry(line, now_utc=SUMMER) == (
        "14:33 EDT - Moda: marked handled"
    )


def test_standard_time_pacific_stamp_converts_too():
    winter = datetime(2026, 1, 15, 19, 40, tzinfo=timezone.utc)
    line = "11:33 PST - Moda: marked handled"
    assert _normalize_audit_log_entry(line, now_utc=winter) == (
        "14:33 EST - Moda: marked handled"
    )


def test_a_line_already_in_eastern_is_left_alone():
    line = "14:33 EDT - Moda: marked handled"
    assert _normalize_audit_log_entry(line, now_utc=SUMMER) == line
