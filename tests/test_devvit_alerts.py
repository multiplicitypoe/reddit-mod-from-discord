import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.devvit_alerts import DevvitAlert, parse_alert  # noqa: E402


def test_a_post_marker_is_read():
    assert parse_alert(["rmfd:v1 t3_abc123 r/examplesub"]) == DevvitAlert("t3_abc123", "examplesub")


def test_a_comment_marker_is_read():
    assert parse_alert(["rmfd:v1 t1_x9 r/ExampleSub2"]) == DevvitAlert("t1_x9", "ExampleSub2")


def test_the_first_embed_with_a_marker_wins():
    assert parse_alert([None, "something else", "rmfd:v1 t3_a r/s", "rmfd:v1 t3_b r/s"]) == DevvitAlert("t3_a", "s")


def test_other_footers_are_ignored():
    for text in ("", None, "rmfd:v2 t3_abc r/s", "rmfd:v1 t5_abc r/s", "rmfd:v1 t3_abc", "x rmfd:v1 t3_abc r/s", "rmfd:v1 t3_abc r/s extra"):
        assert parse_alert([text]) is None
