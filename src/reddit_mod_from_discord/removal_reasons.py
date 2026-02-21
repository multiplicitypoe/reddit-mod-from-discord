from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import unquote

from reddit_mod_from_discord.models import ThingKind

RemovalReasonsSource = Literal["toolbox_wiki", "subreddit_rules", "none"]

_TOOLBOX_UNICODE_RE = re.compile(r"%u(?P<code>[0-9a-fA-F]{4})")
_TITLE_KEY_RE = re.compile(r"^\s*(?P<key>[0-9]+[a-zA-Z]?)\)\s*(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class RemovalReason:
    key: str
    title: str
    text: str
    remove_posts: bool
    remove_comments: bool

    def applies_to(self, kind: ThingKind) -> bool:
        if kind == "submission":
            return self.remove_posts
        return self.remove_comments


@dataclass(frozen=True)
class RemovalReasonSet:
    source: RemovalReasonsSource
    header: str
    footer: str
    reasons: list[RemovalReason]

    def applicable_reasons(self, kind: ThingKind) -> list[RemovalReason]:
        return [reason for reason in self.reasons if reason.applies_to(kind)]


def _toolbox_decode(value: str) -> str:
    # Toolbox exports percent-encoded strings and sometimes legacy %uXXXX sequences.
    text = unquote(value)

    def _replace(m: re.Match[str]) -> str:
        try:
            return chr(int(m.group("code"), 16))
        except Exception:
            return m.group(0)

    text = _TOOLBOX_UNICODE_RE.sub(_replace, text)
    return text


def _extract_key_and_title(raw_title: str, fallback_key: str) -> tuple[str, str]:
    title = _toolbox_decode(raw_title).strip()
    match = _TITLE_KEY_RE.match(title)
    if match is None:
        return fallback_key, title
    key = match.group("key").strip().lower()
    return key, match.group("title").strip()


def parse_toolbox_wiki_payload(raw: str, *, kind: ThingKind) -> RemovalReasonSet | None:
    """
    Parse a /wiki/toolbox JSON blob (Toolbox removal reasons).
    Returns None if payload isn't valid JSON or doesn't contain removalReasons.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    removal = payload.get("removalReasons")
    if not isinstance(removal, dict):
        return None

    header_raw = removal.get("header")
    footer_raw = removal.get("footer")
    header = _toolbox_decode(header_raw) if isinstance(header_raw, str) else ""
    footer = _toolbox_decode(footer_raw) if isinstance(footer_raw, str) else ""

    reasons_raw = removal.get("reasons")
    if not isinstance(reasons_raw, list):
        return None

    reasons: list[RemovalReason] = []
    for idx, entry in enumerate(reasons_raw, start=1):
        if not isinstance(entry, dict):
            continue
        raw_title = entry.get("title")
        raw_text = entry.get("text")
        if not isinstance(raw_title, str) or not raw_title.strip():
            continue
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue

        key, title = _extract_key_and_title(raw_title, f"r{idx}")
        text = _toolbox_decode(raw_text).strip()
        remove_posts = bool(entry.get("removePosts", True))
        remove_comments = bool(entry.get("removeComments", True))
        reason = RemovalReason(
            key=key,
            title=title,
            text=text,
            remove_posts=remove_posts,
            remove_comments=remove_comments,
        )
        if reason.applies_to(kind):
            reasons.append(reason)

    if not reasons:
        return None
    return RemovalReasonSet(
        source="toolbox_wiki",
        header=header.strip(),
        footer=footer.strip(),
        reasons=reasons,
    )


def parse_subreddit_rules(
    rules: Iterable[dict[str, str | None]],
    *,
    kind: ThingKind,
) -> RemovalReasonSet | None:
    parsed: list[RemovalReason] = []
    for idx, rule in enumerate(rules, start=1):
        raw_title = rule.get("short_name") or ""
        raw_text = rule.get("description") or ""
        title = str(raw_title).strip()
        text = str(raw_text).strip()
        if not title and not text:
            continue
        key = f"r{idx}"
        parsed.append(
            RemovalReason(
                key=key,
                title=title or key,
                text=text,
                remove_posts=True,
                remove_comments=True,
            )
        )

    if not parsed:
        return None
    return RemovalReasonSet(
        source="subreddit_rules",
        header="",
        footer="",
        reasons=parsed,
    )


def render_removal_message(
    reason_set: RemovalReasonSet,
    reason: RemovalReason,
    *,
    title: str,
    url: str,
) -> str:
    header = reason_set.header
    footer = reason_set.footer
    parts: list[str] = []

    if header:
        parts.append(header.replace("{title}", title).replace("{url}", url))

    parts.append(reason.text)

    if footer:
        parts.append(footer)

    message = "\n\n".join(part.strip() for part in parts if part and part.strip())
    return message.strip()

