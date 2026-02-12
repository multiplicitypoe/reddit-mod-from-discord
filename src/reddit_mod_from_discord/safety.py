from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_MAX_URL_LENGTH = 2048


def sanitize_http_url(raw_url: str | None) -> str | None:
    if not isinstance(raw_url, str):
        return None

    url = raw_url.strip()
    if not url:
        return None
    if len(url) > _MAX_URL_LENGTH:
        return None
    if any(ord(ch) < 32 for ch in url):
        return None
    if any(ch.isspace() for ch in url):
        return None

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None

    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))
