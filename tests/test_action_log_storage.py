"""Action log lines are stored in UTC and converted only when rendered.

Storing a local time bakes the display zone into the data, so changing the zone
later leaves old lines wrong and undoes nothing. Storing UTC with a full date
keeps the stored line true forever and makes the display zone a rendering
decision.
"""
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.discord_ui import report_view as rv  # noqa: E402

SUMMER = datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc)
WINTER = datetime(2026, 1, 15, 18, 40, tzinfo=timezone.utc)


def test_stamp_is_utc_with_a_full_date():
    assert rv._utc_stamp(SUMMER) == "2026-08-17 18:40 UTC"


def test_stamp_is_in_the_format_the_renderer_parses():
    line = "%s - Moda: marked handled" % rv._utc_stamp(SUMMER)
    assert rv._UTC_STAMP_WITH_DATE_RE.match(line) is not None


def test_a_stored_line_renders_in_the_display_zone():
    line = "%s - Moda: marked handled" % rv._utc_stamp(SUMMER)
    assert rv._normalize_audit_log_entry(line) == "14:40 EDT - Moda: marked handled"


def test_a_winter_line_renders_in_standard_time():
    line = "%s - Moda: marked handled" % rv._utc_stamp(WINTER)
    assert rv._normalize_audit_log_entry(line) == "13:40 EST - Moda: marked handled"


def test_the_same_stored_line_follows_whatever_the_display_zone_is(monkeypatch):
    """The point of storing UTC: changing the zone needs no data migration."""
    stored = "2026-08-17 18:40 UTC - Moda: marked handled"

    monkeypatch.setattr(rv, "_DISPLAY_TZ", ZoneInfo("America/Los_Angeles"))
    assert rv._normalize_audit_log_entry(stored) == "11:40 PDT - Moda: marked handled"

    monkeypatch.setattr(rv, "_DISPLAY_TZ", ZoneInfo("America/Chicago"))
    assert rv._normalize_audit_log_entry(stored) == "13:40 CDT - Moda: marked handled"

    monkeypatch.setattr(rv, "_DISPLAY_TZ", ZoneInfo("America/New_York"))
    assert rv._normalize_audit_log_entry(stored) == "14:40 EDT - Moda: marked handled"
