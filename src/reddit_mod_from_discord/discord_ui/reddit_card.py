"""Render a reported comment or reported post as a PNG.

A reported comment renders as a Reddit-style comment card: full post
title, what kind of post it's under (text body preview or link domain —
so a reviewer never has to guess whether there's more to the post than
the title), the comment being replied to (if any), then the comment that
was actually reported, in its own tinted box labeled REPORTED COMMENT.

A reported post is a different shape, deliberately not the comment
layout with a name/avatar bolted on - a post isn't "posted by X: text"
the way a comment reply reads, and reusing that treatment made the two
report types look confusingly similar. Instead it mirrors Discord's own
native Reddit-link embed: subreddit line, title, body preview or link
domain, a small stats row - all inside one box labeled REPORTED POST.

No vote arrows or action buttons on either - those would look
interactive without being real, which is exactly what this is avoiding.

Typeface is Clarity City (VMware's open-source family, SIL OFL, bundled
under assets/fonts/): an uppercase, wide-tracked eyebrow for the
subreddit line, an extrabold title, and one confident family carrying
the whole card rather than a generic system sans doing double duty.

Everything is drawn at 2x (_SCALE): Discord's embed column is narrower
than a 600px-logical card, so it always downsamples the image somewhat,
and text drawn at native size came out visibly soft after that resize.
Rendering at double resolution and letting Discord scale it down is the
same trick as a retina/@2x image asset.

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
import zlib
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

from reddit_mod_from_discord.models import ReportViewPayload

logger = logging.getLogger("reddit_mod_from_discord")

_SCALE = 2


def _s(px: float) -> int:
    return round(px * _SCALE)


_WIDTH = _s(600)
_PAD = _s(24)
_RADIUS = _s(14)

_BG = "#FFFFFF"
_CARD_BORDER = "#EDEFF1"
_INK = "#1A1A1B"
_MUTED = "#787C7E"
_SUBTLE = "#B0B3B5"
_CONTEXT_BG = "#F6F7F8"
_FLAG_BG = "#FCEAE8"
_FLAG_ACCENT = "#B5312A"

# Deliberately no pure red or green: those are the embed's own status colors
# (removed / handled) elsewhere on the card, and a per-user avatar color
# landing on one by chance would look like a status signal it isn't.
_AVATAR_COLORS = ["#0079D3", "#2B6CB0", "#7E53C1", "#6B46C1", "#FFB000", "#B5502F"]

_FONT_DIR = "/app/assets/fonts/clarity-city"
_FONT_PATHS = {
    "regular": f"{_FONT_DIR}/ClarityCity-Regular.ttf",
    "medium": f"{_FONT_DIR}/ClarityCity-Medium.ttf",
    "semibold": f"{_FONT_DIR}/ClarityCity-SemiBold.ttf",
    "bold": f"{_FONT_DIR}/ClarityCity-Bold.ttf",
    "extrabold": f"{_FONT_DIR}/ClarityCity-ExtraBold.ttf",
}


@functools.lru_cache(maxsize=24)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATHS[weight], _s(size))


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


def _draw_tracked(
    draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont,
    fill: str, tracking: float = 0,
) -> float:
    """Draw text letter-by-letter with extra spacing between characters —
    Pillow has no built-in tracking/letter-spacing control. `tracking` is in
    logical (pre-scale) px. Returns the x position after the last character."""
    x, y = xy
    tracking_px = _s(tracking)
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking_px
    return x


def _avatar_color(username: str) -> str:
    # Python's built-in hash() is randomized per-process (PYTHONHASHSEED),
    # so it gave a different color to the same user every time the bot
    # restarted. crc32 is stable across runs, which is the actual point of
    # a per-user color - the same person should always get the same one.
    return _AVATAR_COLORS[zlib.crc32(username.encode("utf-8")) % len(_AVATAR_COLORS)]


def _draw_avatar(draw: ImageDraw.ImageDraw, x: int, y: int, d: int, username: str) -> None:
    """`d` is already in scaled px (a computed layout size, not a logical
    constant), so it's used as-is rather than passed through _s again."""
    draw.ellipse([x, y, x + d, y + d], fill=_avatar_color(username))
    initial = (username or "?")[:1].upper()
    font = ImageFont.truetype(_FONT_PATHS["bold"], max(_s(10), d // 2))
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


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc
    except Exception:
        return None
    return netloc[4:] if netloc.startswith("www.") else netloc or None


def render_reddit_card(payload: ReportViewPayload) -> bytes | None:
    try:
        if payload.kind == "submission":
            return _render_submission(payload)
        return _render_comment(payload)
    except Exception:
        logger.exception("Failed to render Reddit card for %s", payload.fullname)
        return None


def _render_comment(payload: ReportViewPayload) -> bytes:
    f_sub = _font("semibold", 13)
    f_title = _font("extrabold", 22)
    f_posttype = _font("medium", 15)
    f_parent_name = _font("semibold", 14)
    f_name = _font("bold", 16)
    f_meta = _font("regular", 13)
    f_body = _font("medium", 18)
    f_pbody = _font("regular", 15)
    f_hint = _font("regular", 12)
    f_flag = _font("bold", 12)

    measure_img = Image.new("RGB", (10, 10))
    measure = ImageDraw.Draw(measure_img)

    content_w = _WIDTH - 2 * _PAD
    title_lines = _wrap(measure, payload.title or "(no title)", f_title, content_w)

    # What kind of post this comment sits under, so a reviewer never has to
    # guess whether there's a body they aren't seeing.
    post_type_lines: list[str] = []
    if payload.post_is_self is True:
        if payload.post_selftext:
            post_type_lines = _wrap(measure, payload.post_selftext, f_posttype, content_w)
        else:
            post_type_lines = ["Text post — no body"]
    elif payload.post_is_self is False:
        domain = _domain(payload.link_url)
        post_type_lines = [f"Link post · {domain}" if domain else "Link post"]

    has_parent = bool(payload.parent_author)
    parent_avatar_d = _s(22)
    parent_indent = parent_avatar_d + _s(10)
    parent_lines: list[str] = []
    if has_parent:
        parent_body = (payload.parent_body or "").strip() or "[no text]"
        parent_lines = _wrap(measure, parent_body, f_pbody, content_w - parent_indent - _s(16))

    # The reported comment gets its own tinted, bordered box - without it,
    # nothing on the card actually marked which part was the reported
    # content versus surrounding context; position and a thin divider
    # weren't enough to read as "this is the flagged thing" at a glance,
    # especially at Discord's shrunk-down preview size.
    comment_avatar_d = _s(34)
    comment_indent = comment_avatar_d + _s(12)
    box_pad = _s(14)
    body_lines = _wrap(measure, payload.snippet or "[no text]", f_body, content_w - comment_indent - 2 * box_pad)
    comment_row_h = max(comment_avatar_d, f_name.size + _s(4)) + _s(2)
    comment_body_h = len(body_lines) * (f_body.size + _s(7))
    flag_box_h = box_pad * 2 + comment_row_h + comment_body_h

    y = _PAD
    y += f_sub.size + _s(8)  # subreddit eyebrow
    y += len(title_lines) * (f_title.size + _s(4)) + _s(4)
    if post_type_lines:
        y += len(post_type_lines) * (f_posttype.size + _s(4)) + _s(6)
    y += _s(20)  # gap before context/reported section, no rule line

    if has_parent:
        if payload.parent_is_nested:
            y += f_hint.size + _s(6)
        y += max(parent_avatar_d, f_parent_name.size + _s(4)) + _s(2)
        y += len(parent_lines) * (f_pbody.size + _s(5)) + _s(14)

    y += f_flag.size + _s(6)  # "REPORTED COMMENT" label
    y += flag_box_h
    y += _PAD

    height = int(y)
    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, _WIDTH - 1, height - 1], radius=_RADIUS, outline=_CARD_BORDER, width=_SCALE)

    cy = _PAD

    _draw_tracked(draw, (_PAD, cy), f"R/{payload.subreddit}".upper(), f_sub, _MUTED, tracking=1.2)
    cy += f_sub.size + _s(8)

    for line in title_lines:
        draw.text((_PAD, cy), line, font=f_title, fill=_INK)
        cy += f_title.size + _s(4)
    cy += _s(4)

    for line in post_type_lines:
        draw.text((_PAD, cy), line, font=f_posttype, fill=_MUTED)
        cy += f_posttype.size + _s(4)
    if post_type_lines:
        cy += _s(2)

    cy += _s(20)

    if has_parent:
        if payload.parent_is_nested:
            draw.text((_PAD, cy), "... replying further up — see full thread", font=f_hint, fill=_SUBTLE)
            cy += f_hint.size + _s(6)
        row_top = cy
        _draw_avatar(draw, _PAD, row_top, parent_avatar_d, payload.parent_author or "?")
        draw.text(
            (_PAD + parent_indent, row_top + parent_avatar_d / 2 - f_parent_name.size / 2 - _s(2)),
            f"u/{payload.parent_author}",
            font=f_parent_name,
            fill=_MUTED,
        )
        cy += max(parent_avatar_d, f_parent_name.size + _s(4)) + _s(2)
        bubble_top = cy - _s(2)
        bubble_h = len(parent_lines) * (f_pbody.size + _s(5)) + _s(10)
        draw.rounded_rectangle(
            [_PAD + parent_indent, bubble_top, _WIDTH - _PAD, bubble_top + bubble_h],
            radius=_s(8), fill=_CONTEXT_BG,
        )
        ty = bubble_top + _s(6)
        for line in parent_lines:
            draw.text((_PAD + parent_indent + _s(10), ty), line, font=f_pbody, fill=_MUTED)
            ty += f_pbody.size + _s(5)
        cy = bubble_top + bubble_h + _s(14)

    _draw_tracked(draw, (_PAD, cy), "REPORTED COMMENT", f_flag, _FLAG_ACCENT, tracking=1.2)
    cy += f_flag.size + _s(6)

    box_top = cy
    draw.rounded_rectangle(
        [_PAD, box_top, _WIDTH - _PAD, box_top + flag_box_h],
        radius=_s(10), fill=_FLAG_BG,
    )

    row_top = box_top + box_pad
    avatar_x = _PAD + box_pad
    text_x = avatar_x + comment_indent
    _draw_avatar(draw, avatar_x, row_top, comment_avatar_d, payload.author or "?")
    name_y = row_top + comment_avatar_d / 2 - f_name.size / 2 - _s(2)
    draw.text((text_x, name_y), f"u/{payload.author or '[deleted]'}", font=f_name, fill=_INK)
    name_w = draw.textlength(f"u/{payload.author or '[deleted]'}", font=f_name)
    age = _age(payload.created_utc)
    if age:
        draw.text(
            (text_x + name_w + _s(8), name_y + _s(2)),
            f"· {age}",
            font=f_meta,
            fill=_MUTED,
        )
    ty = row_top + comment_row_h
    for line in body_lines:
        draw.text((text_x, ty), line, font=f_body, fill=_INK)
        ty += f_body.size + _s(7)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_submission(payload: ReportViewPayload) -> bytes:
    """A reported post is not "posted by X: text" the way a comment reply
    reads - the whole post is the reported thing, so it gets Discord's own
    native Reddit-link-embed shape instead of the comment card's avatar
    treatment: subreddit line outside, then title / body-or-link / a small
    stats row, all together inside one REPORTED POST box."""
    f_sub = _font("semibold", 13)
    f_flag = _font("bold", 12)
    f_title = _font("extrabold", 22)
    f_body = _font("medium", 16)
    f_stats = _font("regular", 13)

    measure_img = Image.new("RGB", (10, 10))
    measure = ImageDraw.Draw(measure_img)

    box_pad = _s(18)
    inner_w = _WIDTH - 2 * _PAD - 2 * box_pad

    title_lines = _wrap(measure, payload.title or "(no title)", f_title, inner_w)

    # The post's own content: its text body if it's a self post, or the
    # link and domain it points to if it's a link post. Comes straight off
    # the payload's own fields - unlike a comment report, there's no parent
    # post to fetch separately, this report *is* the post.
    body_lines: list[str] = []
    is_link = False
    domain = None
    snippet = (payload.snippet or "").strip()
    if snippet:
        body_lines = _wrap(measure, snippet, f_body, inner_w)
    else:
        is_link = bool(
            payload.link_url
            and payload.link_url != payload.permalink
            and payload.link_url != payload.media_url
        )
        if is_link:
            domain = _domain(payload.link_url)

    y = _PAD
    y += f_sub.size + _s(8)  # subreddit eyebrow
    y += f_flag.size + _s(6)  # "REPORTED POST" label

    box_h = box_pad
    box_h += len(title_lines) * (f_title.size + _s(4))
    box_h += _s(10)
    if body_lines:
        box_h += len(body_lines) * (f_body.size + _s(6))
        box_h += _s(4)
    elif is_link:
        box_h += f_body.size + _s(4)
        box_h += _s(4)
    if payload.num_comments is not None:
        box_h += _s(10)
        box_h += f_stats.size
    box_h += box_pad

    y += box_h
    y += _PAD

    height = int(y)
    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, _WIDTH - 1, height - 1], radius=_RADIUS, outline=_CARD_BORDER, width=_SCALE)

    cy = _PAD
    _draw_tracked(draw, (_PAD, cy), f"R/{payload.subreddit}".upper(), f_sub, _MUTED, tracking=1.2)
    cy += f_sub.size + _s(8)

    _draw_tracked(draw, (_PAD, cy), "REPORTED POST", f_flag, _FLAG_ACCENT, tracking=1.2)
    cy += f_flag.size + _s(6)

    box_top = cy
    draw.rounded_rectangle(
        [_PAD, box_top, _WIDTH - _PAD, box_top + box_h],
        radius=_s(10), fill=_FLAG_BG,
    )

    ty = box_top + box_pad
    text_x = _PAD + box_pad
    for line in title_lines:
        draw.text((text_x, ty), line, font=f_title, fill=_INK)
        ty += f_title.size + _s(4)
    ty += _s(10)

    if body_lines:
        for line in body_lines:
            draw.text((text_x, ty), line, font=f_body, fill=_INK)
            ty += f_body.size + _s(6)
        ty += _s(4)
    elif is_link:
        label = f"Link post · {domain}" if domain else "Link post"
        draw.text((text_x, ty), label, font=f_body, fill=_MUTED)
        ty += f_body.size + _s(4)
        ty += _s(4)

    if payload.num_comments is not None:
        ty += _s(10)
        draw.text((text_x, ty), f"{payload.num_comments} comments", font=f_stats, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
