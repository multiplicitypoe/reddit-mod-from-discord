from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_mod_channel_id: int
    discord_allowed_role_ids: tuple[int, ...]
    reddit_client_id: str
    reddit_client_secret: str
    reddit_refresh_token: str | None
    reddit_username: str | None
    reddit_password: str | None
    reddit_redirect_uri: str
    reddit_user_agent: str
    reddit_subreddit: str
    poll_interval_minutes: int
    post_report_threshold: int
    comment_report_threshold: int
    max_reports_per_poll: int
    db_path: str
    view_store_ttl_hours: int
    debug_logs: bool


def _env_optional(name: str) -> str | None:
    return os.getenv(name)


def _env(name: str, default: str) -> str:
    value = _env_optional(name)
    if value is None:
        return default
    return value


def _env_int(name: str, default: int) -> int:
    value = _env_optional(name)
    if value is None:
        return default
    return int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = _env_optional(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _required(name: str) -> str:
    value = _env_optional(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _parse_role_ids(raw: str) -> tuple[int, ...]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    role_ids: list[int] = []
    for part in parts:
        role_ids.append(int(part))
    return tuple(role_ids)


def load_settings() -> Settings:
    role_ids_raw = _env(
        "DISCORD_ALLOWED_ROLE_IDS",
        "1221785711922122792,604756836847059015",
    )
    role_ids = _parse_role_ids(role_ids_raw)

    reddit_refresh_token = _env_optional("REDDIT_REFRESH_TOKEN")
    reddit_username = _env_optional("REDDIT_USERNAME")
    reddit_password = _env_optional("REDDIT_PASSWORD")
    reddit_redirect_uri = _env("REDDIT_REDIRECT_URI", "http://localhost:8080")

    if not reddit_refresh_token:
        if not (reddit_username and reddit_password):
            raise ValueError(
                "Set REDDIT_REFRESH_TOKEN, or set both REDDIT_USERNAME and REDDIT_PASSWORD"
            )

    return Settings(
        discord_token=_required("DISCORD_TOKEN"),
        discord_mod_channel_id=_env_int("DISCORD_MOD_CHANNEL_ID", 604768963741876255),
        discord_allowed_role_ids=role_ids,
        reddit_client_id=_required("REDDIT_CLIENT_ID"),
        reddit_client_secret=_required("REDDIT_CLIENT_SECRET"),
        reddit_refresh_token=reddit_refresh_token or None,
        reddit_username=reddit_username or None,
        reddit_password=reddit_password or None,
        reddit_redirect_uri=reddit_redirect_uri,
        reddit_user_agent=_env(
            "REDDIT_USER_AGENT",
            "reddit-mod-from-discord/0.1 by u/your_username",
        ),
        reddit_subreddit=_env("REDDIT_SUBREDDIT", "codelyoko"),
        poll_interval_minutes=max(_env_int("POLL_INTERVAL_MINUTES", 5), 1),
        post_report_threshold=max(_env_int("POST_REPORT_THRESHOLD", 1), 1),
        comment_report_threshold=max(_env_int("COMMENT_REPORT_THRESHOLD", 1), 1),
        max_reports_per_poll=max(_env_int("MAX_REPORTS_PER_POLL", 100), 1),
        db_path=_env("DB_PATH", "data/reddit_mod_from_discord.sqlite3"),
        view_store_ttl_hours=max(_env_int("VIEW_STORE_TTL_HOURS", 168), 1),
        debug_logs=_env_bool("DEBUG_LOGS", False),
    )
