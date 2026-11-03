"""Alerts posted by the Devvit app, which the bot turns into its own cards.

A webhook message cannot carry buttons, so the Devvit app posts an embed whose
footer reads ``rmfd:v1 <fullname> r/<subreddit>``. The bot recognizes that
marker, posts its own card for the item, then deletes the webhook original.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MARKER = "rmfd:v1"
_FOOTER = re.compile(rf"^{re.escape(MARKER)} (t[13]_[a-z0-9]+) r/([A-Za-z0-9_]+)$")


@dataclass(frozen=True)
class DevvitAlert:
    fullname: str
    subreddit: str


def parse_alert(footer_texts: Iterable[str | None]) -> DevvitAlert | None:
    """The first footer that carries the marker, or None."""
    for text in footer_texts:
        if not text:
            continue
        match = _FOOTER.match(text.strip())
        if match:
            return DevvitAlert(fullname=match.group(1), subreddit=match.group(2))
    return None
