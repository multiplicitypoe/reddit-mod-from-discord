from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_mod_channel_id: int | None
    discord_allowed_role_ids: tuple[int, ...] | None
    discord_silent_notifications: bool

    demo_mode: bool
    demo_post_url: str

    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_refresh_token: str | None
    reddit_username: str | None
    reddit_password: str | None
    reddit_redirect_uri: str
    reddit_user_agent: str
    reddit_subreddit: str | None
    poll_interval_minutes: int
    post_report_threshold: int
    comment_report_threshold: int
    max_reports_per_poll: int
    max_item_age_hours: int
    modlog_fetch_limit: int
    db_path: str
    view_store_ttl_hours: int
    debug_logs: bool
    multi_server_config_path: str | None
    multi_server_config: dict[str, "SetupConfig"]


@dataclass(frozen=True)
class SetupConfig:
    setup_id: str
    guild_id: int
    overrides: "SettingsOverrides"


@dataclass(frozen=True)
class ResolvedSettings:
    discord_token: str
    discord_mod_channel_id: int | None
    discord_allowed_role_ids: tuple[int, ...] | None
    discord_silent_notifications: bool

    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_refresh_token: str | None
    reddit_username: str | None
    reddit_password: str | None
    reddit_redirect_uri: str
    reddit_user_agent: str
    reddit_subreddit: str | None
    poll_interval_minutes: int
    post_report_threshold: int
    comment_report_threshold: int
    max_reports_per_poll: int
    max_item_age_hours: int
    modlog_fetch_limit: int
    debug_logs: bool


UNSET = object()

MULTI_SERVER_ALLOWED_KEYS = {
    "discord_guild_id",
    "discord_mod_channel_id",
    "discord_allowed_role_ids",
    "discord_silent_notifications",
    "reddit_client_id",
    "reddit_client_secret",
    "reddit_refresh_token",
    "reddit_username",
    "reddit_password",
    "reddit_redirect_uri",
    "reddit_user_agent",
    "reddit_subreddit",
    "poll_interval_minutes",
    "post_report_threshold",
    "comment_report_threshold",
    "max_reports_per_poll",
    "max_item_age_hours",
    "modlog_fetch_limit",
}


@dataclass(frozen=True)
class SettingsOverrides:
    discord_mod_channel_id: int | None | object = UNSET
    discord_allowed_role_ids: tuple[int, ...] | None | object = UNSET
    discord_silent_notifications: bool | None | object = UNSET
    reddit_client_id: str | None | object = UNSET
    reddit_client_secret: str | None | object = UNSET
    reddit_refresh_token: str | None | object = UNSET
    reddit_username: str | None | object = UNSET
    reddit_password: str | None | object = UNSET
    reddit_redirect_uri: str | None | object = UNSET
    reddit_user_agent: str | None | object = UNSET
    reddit_subreddit: str | None | object = UNSET
    poll_interval_minutes: int | None | object = UNSET
    post_report_threshold: int | None | object = UNSET
    comment_report_threshold: int | None | object = UNSET
    max_reports_per_poll: int | None | object = UNSET
    max_item_age_hours: int | None | object = UNSET
    modlog_fetch_limit: int | None | object = UNSET


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


def _as_optional_str(value: Any) -> str | None | object:
    if value is UNSET:
        return UNSET
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_optional_int(value: Any) -> int | None | object:
    if value is UNSET:
        return UNSET
    if value is None:
        return None
    return int(value)


def _as_optional_bool(value: Any) -> bool | None | object:
    if value is UNSET:
        return UNSET
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_optional_role_ids(value: Any) -> tuple[int, ...] | None | object:
    if value is UNSET:
        return UNSET
    if value is None:
        return None
    if isinstance(value, str):
        return _parse_role_ids(value)
    if isinstance(value, (list, tuple)):
        return tuple(int(part) for part in value)
    return (int(value),)


def _parse_multi_server_overrides(payload: dict[str, Any]) -> SettingsOverrides:
    return SettingsOverrides(
        discord_mod_channel_id=_as_optional_int(payload.get("discord_mod_channel_id", UNSET)),
        discord_allowed_role_ids=_as_optional_role_ids(
            payload.get("discord_allowed_role_ids", UNSET)
        ),
        discord_silent_notifications=_as_optional_bool(
            payload.get("discord_silent_notifications", UNSET)
        ),
        reddit_client_id=_as_optional_str(payload.get("reddit_client_id", UNSET)),
        reddit_client_secret=_as_optional_str(payload.get("reddit_client_secret", UNSET)),
        reddit_refresh_token=_as_optional_str(payload.get("reddit_refresh_token", UNSET)),
        reddit_username=_as_optional_str(payload.get("reddit_username", UNSET)),
        reddit_password=_as_optional_str(payload.get("reddit_password", UNSET)),
        reddit_redirect_uri=_as_optional_str(payload.get("reddit_redirect_uri", UNSET)),
        reddit_user_agent=_as_optional_str(payload.get("reddit_user_agent", UNSET)),
        reddit_subreddit=_as_optional_str(payload.get("reddit_subreddit", UNSET)),
        poll_interval_minutes=_as_optional_int(payload.get("poll_interval_minutes", UNSET)),
        post_report_threshold=_as_optional_int(payload.get("post_report_threshold", UNSET)),
        comment_report_threshold=_as_optional_int(payload.get("comment_report_threshold", UNSET)),
        max_reports_per_poll=_as_optional_int(payload.get("max_reports_per_poll", UNSET)),
        max_item_age_hours=_as_optional_int(payload.get("max_item_age_hours", UNSET)),
        modlog_fetch_limit=_as_optional_int(payload.get("modlog_fetch_limit", UNSET)),
    )


def _extract_guild_id(setup_id: str, payload: dict[str, Any]) -> int:
    raw = payload.get("discord_guild_id", UNSET)
    if raw is not UNSET:
        try:
            return int(raw)
        except Exception as exc:
            raise ValueError(f"discord_guild_id for {setup_id} must be an integer") from exc
    if setup_id.isdigit():
        return int(setup_id)
    raise ValueError(f"discord_guild_id is required for setup {setup_id}")


def _load_multi_server_config(path: str | None) -> dict[str, SetupConfig]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MULTI_SERVER_CONFIG_PATH must point to a JSON object")
    config: dict[str, SetupConfig] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Multi server config keys must be non-empty strings")
        if not isinstance(value, dict):
            raise ValueError(f"Config for setup {key} must be an object")
        unknown_keys = sorted(set(value.keys()) - MULTI_SERVER_ALLOWED_KEYS)
        if unknown_keys:
            raise ValueError(
                f"Unknown keys in multi server config for setup {key}: {', '.join(unknown_keys)}"
            )
        guild_id = _extract_guild_id(key, value)
        override_payload = dict(value)
        override_payload.pop("discord_guild_id", None)
        config[key] = SetupConfig(
            setup_id=key,
            guild_id=guild_id,
            overrides=_parse_multi_server_overrides(override_payload),
        )
    return config


def _resolve_value(override: Any | object, fallback: Any) -> Any:
    return fallback if override is UNSET else override


def _resolve_required(name: str, override: Any | object, fallback: Any) -> Any:
    if override is UNSET:
        return fallback
    if override is None:
        raise ValueError(f"{name} cannot be null")
    return override


def resolve_settings(base: Settings, overrides: SettingsOverrides | None) -> ResolvedSettings:
    if not overrides:
        return ResolvedSettings(
            discord_token=base.discord_token,
            discord_mod_channel_id=base.discord_mod_channel_id,
            discord_allowed_role_ids=base.discord_allowed_role_ids,
            discord_silent_notifications=base.discord_silent_notifications,
            reddit_client_id=base.reddit_client_id,
            reddit_client_secret=base.reddit_client_secret,
            reddit_refresh_token=base.reddit_refresh_token,
            reddit_username=base.reddit_username,
            reddit_password=base.reddit_password,
            reddit_redirect_uri=base.reddit_redirect_uri,
            reddit_user_agent=base.reddit_user_agent,
            reddit_subreddit=base.reddit_subreddit,
        poll_interval_minutes=base.poll_interval_minutes,
        post_report_threshold=base.post_report_threshold,
        comment_report_threshold=base.comment_report_threshold,
        max_reports_per_poll=base.max_reports_per_poll,
        max_item_age_hours=base.max_item_age_hours,
        modlog_fetch_limit=base.modlog_fetch_limit,
        debug_logs=base.debug_logs,
    )

    return ResolvedSettings(
        discord_token=base.discord_token,
        discord_mod_channel_id=_resolve_required(
            "discord_mod_channel_id",
            overrides.discord_mod_channel_id,
            base.discord_mod_channel_id,
        ),
        discord_allowed_role_ids=_resolve_required(
            "discord_allowed_role_ids",
            overrides.discord_allowed_role_ids,
            base.discord_allowed_role_ids,
        ),
        discord_silent_notifications=_resolve_required(
            "discord_silent_notifications",
            overrides.discord_silent_notifications,
            base.discord_silent_notifications,
        ),
        reddit_client_id=_resolve_required(
            "reddit_client_id",
            overrides.reddit_client_id,
            base.reddit_client_id,
        ),
        reddit_client_secret=_resolve_required(
            "reddit_client_secret",
            overrides.reddit_client_secret,
            base.reddit_client_secret,
        ),
        reddit_refresh_token=_resolve_value(
            overrides.reddit_refresh_token,
            base.reddit_refresh_token,
        ),
        reddit_username=_resolve_value(overrides.reddit_username, base.reddit_username),
        reddit_password=_resolve_value(overrides.reddit_password, base.reddit_password),
        reddit_redirect_uri=_resolve_required(
            "reddit_redirect_uri",
            overrides.reddit_redirect_uri,
            base.reddit_redirect_uri,
        ),
        reddit_user_agent=_resolve_required(
            "reddit_user_agent",
            overrides.reddit_user_agent,
            base.reddit_user_agent,
        ),
        reddit_subreddit=_resolve_required(
            "reddit_subreddit",
            overrides.reddit_subreddit,
            base.reddit_subreddit,
        ),
        poll_interval_minutes=_resolve_required(
            "poll_interval_minutes",
            overrides.poll_interval_minutes,
            base.poll_interval_minutes,
        ),
        post_report_threshold=_resolve_required(
            "post_report_threshold",
            overrides.post_report_threshold,
            base.post_report_threshold,
        ),
        comment_report_threshold=_resolve_required(
            "comment_report_threshold",
            overrides.comment_report_threshold,
            base.comment_report_threshold,
        ),
        max_reports_per_poll=_resolve_required(
            "max_reports_per_poll",
            overrides.max_reports_per_poll,
            base.max_reports_per_poll,
        ),
        max_item_age_hours=_resolve_required(
            "max_item_age_hours",
            overrides.max_item_age_hours,
            base.max_item_age_hours,
        ),
        modlog_fetch_limit=_resolve_required(
            "modlog_fetch_limit",
            overrides.modlog_fetch_limit,
            base.modlog_fetch_limit,
        ),
        debug_logs=base.debug_logs,
    )


def load_settings() -> Settings:
    multi_server_config_path = _env_optional("MULTI_SERVER_CONFIG_PATH")

    role_ids_raw = _env_optional("DISCORD_ALLOWED_ROLE_IDS")
    if not role_ids_raw and not multi_server_config_path:
        role_ids_raw = "1221785711922122792,604756836847059015"
    role_ids = _parse_role_ids(role_ids_raw) if role_ids_raw else None

    discord_mod_channel_id: int | None
    if multi_server_config_path:
        channel_raw = _env_optional("DISCORD_MOD_CHANNEL_ID")
        discord_mod_channel_id = int(channel_raw) if channel_raw else None
    else:
        discord_mod_channel_id = _env_int("DISCORD_MOD_CHANNEL_ID", 604768963741876255)

    demo_mode = _env_bool("DEMO_MODE", False)
    demo_post_url = _env(
        "DEMO_POST_URL",
        "https://old.reddit.com/r/example/comments/1qufcnu/code_lyoko_chatgpt/",
    )

    reddit_client_id = _env_optional("REDDIT_CLIENT_ID")
    reddit_client_secret = _env_optional("REDDIT_CLIENT_SECRET")
    reddit_refresh_token = _env_optional("REDDIT_REFRESH_TOKEN")
    reddit_username = _env_optional("REDDIT_USERNAME")
    reddit_password = _env_optional("REDDIT_PASSWORD")
    reddit_redirect_uri = _env("REDDIT_REDIRECT_URI", "http://localhost:8080")

    if not demo_mode and not multi_server_config_path:
        if not reddit_client_id:
            raise ValueError("REDDIT_CLIENT_ID is required")
        if not reddit_client_secret:
            raise ValueError("REDDIT_CLIENT_SECRET is required")
        if not reddit_refresh_token and not (reddit_username and reddit_password):
            raise ValueError(
                "Set REDDIT_REFRESH_TOKEN, or set both REDDIT_USERNAME and REDDIT_PASSWORD"
            )

    multi_server_config = _load_multi_server_config(multi_server_config_path)

    reddit_subreddit_default = "add_a_subreddit_here_or_this_wont_work"
    if multi_server_config_path:
        reddit_subreddit_default = ""
    reddit_subreddit = _env("REDDIT_SUBREDDIT", reddit_subreddit_default) or None

    return Settings(
        discord_token=_required("DISCORD_TOKEN"),
        discord_mod_channel_id=discord_mod_channel_id,
        discord_allowed_role_ids=role_ids,
        discord_silent_notifications=_env_bool("DISCORD_SILENT_NOTIFICATIONS", True),

        demo_mode=demo_mode,
        demo_post_url=demo_post_url,

        reddit_client_id=reddit_client_id,
        reddit_client_secret=reddit_client_secret,
        reddit_refresh_token=reddit_refresh_token or None,
        reddit_username=reddit_username or None,
        reddit_password=reddit_password or None,
        reddit_redirect_uri=reddit_redirect_uri,
        reddit_user_agent=_env(
            "REDDIT_USER_AGENT",
            "reddit-mod-from-discord/0.1 by u/your_username",
        ),
        reddit_subreddit=reddit_subreddit,
        poll_interval_minutes=max(_env_int("POLL_INTERVAL_MINUTES", 5), 1),
        post_report_threshold=max(_env_int("POST_REPORT_THRESHOLD", 1), 1),
        comment_report_threshold=max(_env_int("COMMENT_REPORT_THRESHOLD", 1), 1),
        max_reports_per_poll=max(_env_int("MAX_REPORTS_PER_POLL", 100), 1),
        max_item_age_hours=max(_env_int("MAX_ITEM_AGE_HOURS", 72), 0),
        modlog_fetch_limit=max(_env_int("MODLOG_FETCH_LIMIT", 50), 0),
        db_path=_env("DB_PATH", "data/reddit_mod_from_discord.sqlite3"),
        view_store_ttl_hours=max(_env_int("VIEW_STORE_TTL_HOURS", 168), 1),
        debug_logs=_env_bool("DEBUG_LOGS", False),
        multi_server_config_path=multi_server_config_path,
        multi_server_config=multi_server_config,
    )
