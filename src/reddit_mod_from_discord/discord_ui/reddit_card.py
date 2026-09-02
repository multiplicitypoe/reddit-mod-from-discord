"""Render a reported comment as a PNG styled like modern Reddit's comment
card: full post title, the comment being replied to (if any), then the
comment that was actually reported. No vote arrows or action buttons — those
would look interactive without being real, which is exactly what this is
avoiding.

Text-only fallback stays in report_view.py if this fails for any reason
(missing fonts, bad input); nothing here should ever be allowed to break
report delivery, so every public entry point catches broadly and returns
None rather than raising.
"""

from __future__ import annotations

import functools
import io
import logging
import time

from PIL import Image, ImageDraw, ImageFont

from reddit_mod_from_discord.models import ReportViewPayload

logger = logging.getLogger("reddit_mod_from_discord")

_WIDTH = 600
_PAD = 24
_RADIUS = 14

_BG = "#FFFFFF"
_CARD_BORDER = "#EDEFF1"
_DIVIDER = "#EDEFF1"
_INK = "#1A1A1B"
_MUTED = "#787C7E"
_SUBTLE = "#B0B3B5"
_CONTEXT_BG = "#F6F7F8"
_SNOO = "#FF4500"

_AVATAR_COLORS = ["#FF4500", "#0079D3", "#46D160", "#FFB000", "#7E53C1", "#019A75", "#EA0027"]

_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@functools.lru_cache(maxsize=16)
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_BOLD if bold else _FONT_REGULAR, size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _avatar_color(username: str) -> str:
    return _AVATAR_COLORS[abs(hash(username)) % len(_AVATAR_COLORS)]


def _draw_avatar(draw: ImageDraw.ImageDraw, x: int, y: int, d: int, username: str) -> None:
    draw.ellipse([x, y, x + d, y + d], fill=_avatar_color(username))
    initial = (username or "?")[:1].upper()
    font = _font(True, max(10, d // 2))
    bbox = draw.textbbox((0, 0), initial, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x + d / 2 - w / 2 - bbox[0], y + d / 2 - h / 2 - bbox[1]), initial, font=font, fill="#FFFFFF")


def _age(ts: float) -> str:
    if ts <= 0:
        return ""
    delta = max(0, int(time.time() - ts))
    if delta < 3600:
        return f"{max(1, delta // 60)}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def render_reddit_card(payload: ReportViewPayload) -> bytes | None:
    try:
        return _render(payload)
    except Exception:
        logger.exception("Failed to render Reddit card for %s", payload.fullname)
        return None


def _render(payload: ReportViewPayload) -> bytes:
    f_sub = _font(True, 12)
    f_title = _font(True, 18)
    f_name = _font(True, 13)
    f_meta = _font(False, 12)
    f_body = _font(False, 15)
    f_pbody = _font(False, 13)
    f_hint = _font(False, 11)

    measure_img = Image.new("RGB", (10, 10))
    measure = ImageDraw.Draw(measure_img)

    content_w = _WIDTH - 2 * _PAD
    title_lines = _wrap(measure, payload.title or "(no title)", f_title, content_w)

    has_parent = bool(payload.parent_author)
    parent_avatar_d = 22
    parent_indent = parent_avatar_d + 10
    parent_lines: list[str] = []
    if has_parent:
        parent_body = (payload.parent_body or "").strip() or "[no text]"
        parent_lines = _wrap(measure, parent_body, f_pbody, content_w - parent_indent - 16)

    comment_avatar_d = 34
    comment_indent = comment_avatar_d + 12
    body_lines = _wrap(measure, payload.snippet or "[no text]", f_body, content_w - comment_indent)

    y = _PAD
    y += f_sub.size + 6  # subreddit line
    y += len(title_lines) * (f_title.size + 6) + 4
    y += 1 + 14  # divider + gap

    if has_parent:
        if payload.parent_is_nested:
            y += f_hint.size + 6
        y += max(parent_avatar_d, f_name.size + 4) + 4
        y += len(parent_lines) * (f_pbody.size + 5) + 14

    y += max(comment_avatar_d, f_name.size + 4) + 6
    y += len(body_lines) * (f_body.size + 6)
    y += _PAD

    height = int(y)
    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, _WIDTH - 1, height - 1], radius=_RADIUS, outline=_CARD_BORDER, width=1)

    cy = _PAD

    draw.ellipse([_PAD, cy + 1, _PAD + 10, cy + 11], fill=_SNOO)
    draw.text((_PAD + 16, cy), f"r/{payload.subreddit}", font=f_sub, fill=_MUTED)
    cy += f_sub.size + 6

    for line in title_lines:
        draw.text((_PAD, cy), line, font=f_title, fill=_INK)
        cy += f_title.size + 6
    cy += 4

    draw.line([(_PAD, cy), (_WIDTH - _PAD, cy)], fill=_DIVIDER, width=1)
    cy += 14

    if has_parent:
        if payload.parent_is_nested:
            draw.text((_PAD, cy), "⋯ replying further up — see full thread", font=f_hint, fill=_SUBTLE)
            cy += f_hint.size + 6
        row_top = cy
        _draw_avatar(draw, _PAD, row_top, parent_avatar_d, payload.parent_author or "?")
        draw.text(
            (_PAD + parent_indent, row_top + parent_avatar_d / 2 - f_name.size / 2 - 2),
            f"u/{payload.parent_author}",
            font=f_name,
            fill=_MUTED,
        )
        cy += max(parent_avatar_d, f_name.size + 4) + 4
        bubble_top = cy - 2
        bubble_h = len(parent_lines) * (f_pbody.size + 5) + 10
        draw.rounded_rectangle(
            [_PAD + parent_indent, bubble_top, _WIDTH - _PAD, bubble_top + bubble_h],
            radius=8, fill=_CONTEXT_BG,
        )
        ty = bubble_top + 6
        for line in parent_lines:
            draw.text((_PAD + parent_indent + 10, ty), line, font=f_pbody, fill=_MUTED)
            ty += f_pbody.size + 5
        cy = bubble_top + bubble_h + 14

    row_top = cy
    _draw_avatar(draw, _PAD, row_top, comment_avatar_d, payload.author or "?")
    name_y = row_top + comment_avatar_d / 2 - f_name.size / 2 - 2
    draw.text((_PAD + comment_indent, name_y), f"u/{payload.author or '[deleted]'}", font=f_name, fill=_INK)
    name_w = draw.textlength(f"u/{payload.author or '[deleted]'}", font=f_name)
    age = _age(payload.created_utc)
    if age:
        draw.text(
            (_PAD + comment_indent + name_w + 8, name_y + 1),
            f"· {age}",
            font=f_meta,
            fill=_MUTED,
        )
    cy += max(comment_avatar_d, f_name.size + 4) + 6

    for line in body_lines:
        draw.text((_PAD + comment_indent, cy), line, font=f_body, fill=_INK)
        cy += f_body.size + 6

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
