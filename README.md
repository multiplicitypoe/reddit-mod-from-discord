# Reddit Mod from Discord

Discord bot that polls Reddit reports for one or more subreddits and posts one actionable alert message per reported item into per-server Discord mod channels.

## What this does

- Polls `r/{subreddit}` report queue on an interval (default 5 minutes per server)
- Posts one Discord message per newly seen reported post/comment
- Dedupes by Reddit fullname (`t3_xxx` submissions, `t1_xxx` comments)
- Provides persistent moderation buttons that survive restarts
- Supports long-text modals for ban message, removal message, and modmail body

## Quick start

1) Create `.env` from example:

```bash
cp .env.example .env
```

2) Fill required values in `.env`:

- `DISCORD_TOKEN`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_REFRESH_TOKEN`

3) Install and run:

```bash
make run-bot
```

## Docker

Build the image (default tag is `latest`):

```bash
make build-docker
```

Run the container in the foreground (this checks for an existing `reddit-mod-from-discord` container, stops/removes it if present, then starts a fresh one):

```bash
make run-docker
```

Useful extras:

```bash
make stop-docker   # stop/remove named container only
make docker        # build image, then run container
```

Notes:
- The image name/tag defaults to `reddit-mod-from-discord:latest`. Override with `make build-docker TAG=...`.
- Data persists in host `data/` via `-v ./data:/app/data`.
- `make run-docker` runs attached; press `Ctrl+C` to stop.

## Environment variables

Required:

- `DISCORD_TOKEN`
- `DISCORD_MOD_CHANNEL_ID`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_REFRESH_TOKEN`
- `DISCORD_ALLOWED_ROLE_IDS`
 
Optional:

- `MULTI_SERVER_CONFIG_PATH` (multi-server only; see appendix below)

If you use `MULTI_SERVER_CONFIG_PATH`, you can omit per-server values from `.env` and set them in the JSON file instead.

Defaults already set in `.env.example`:

- `DISCORD_SILENT_NOTIFICATIONS=true`

Note: `DISCORD_ALLOWED_ROLE_IDS` are server-specific. If you move the bot to a new Discord server, you must update this list to the new server's role IDs.
- `REDDIT_SUBREDDIT=`
- `POLL_INTERVAL_MINUTES=5`
- `POST_REPORT_THRESHOLD=1`
- `COMMENT_REPORT_THRESHOLD=1`
- `MAX_REPORTS_PER_POLL=100`
- `MAX_ITEM_AGE_HOURS=72`
- `MODLOG_FETCH_LIMIT=50`
- `DB_PATH=data/reddit_mod_from_discord.sqlite3`
- `VIEW_STORE_TTL_HOURS=168`

## Slash commands

- `/modsync` - trigger an immediate poll cycle
- `/modhealth` - basic runtime status

## Reddit auth note

Actions run as the Reddit account that authorized the refresh token. If you want actions to come from a dedicated mod account, generate the refresh token while logged into that dedicated account.

## Obtaining a refresh token

If your account cannot create apps yet: Reddit now requires you to register for API access before creating credentials. See:

- https://www.reddit.com/r/reddit.com/wiki/api/

Run the token helper on the host machine (not inside the bot container) because it listens for the OAuth callback on local `http://localhost:8080` by default.

1) Create a Reddit app as a **web app** with redirect URI `http://localhost:8080`.
2) Put `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` into `.env`.
   - If your app uses a different redirect URI, also set `REDDIT_REDIRECT_URI`.
3) Run:

```bash
make ensure-venv install
set -a && . ./.env && set +a
PYTHONPATH=src .venv/bin/python tools/obtain_refresh_token.py
```

Then copy the printed `REDDIT_REFRESH_TOKEN` into `.env`.

## Optional: password auth

If you prefer password flow (not recommended for dedicated mod accounts and long-running services), you can set:

- `REDDIT_USERNAME`
- `REDDIT_PASSWORD`

If `REDDIT_REFRESH_TOKEN` is set, it takes precedence.

## Appendix: Multi-server configuration (advanced)

If you run one bot instance across multiple Discord servers (or multiple subreddits within the same server), you can provide per-setup overrides in a JSON file and point to it with `MULTI_SERVER_CONFIG_PATH`. This is optional; single-server setup with `.env` is still the recommended path.

`cp multi_server_config.json.example multi_server_config.json`, and then in your `.env`, edit it to point at it. `make run-docker` will automatically mount the file if it exists:

```bash
MULTI_SERVER_CONFIG_PATH=multi_server_config.json
DISCORD_TOKEN=xxx
```

Each top-level key is a setup id string. Values override any env defaults for that setup. Include `discord_guild_id` for each setup (unless the setup id is itself a guild id). You can define multiple setups that point at the same `discord_guild_id` to support multiple subreddits in one server. The only settings that cannot be overridden are the Discord bot token, DB path, view TTL, demo mode options, and debug log flag.

If any setup is missing required settings after merging defaults + overrides, the bot will refuse to start and log the missing setup IDs.

When an alert is created for an item that is already approved/removed/locked/ignored in Reddit, the bot will try to fetch recent mod-log entries for that item (up to `MODLOG_FETCH_LIMIT`) and include them in the audit log section.

Example JSON:

```json
{
  "example": {
    "discord_guild_id": 123456789012345678,
    "discord_mod_channel_id": 111111111111111111,
    "discord_allowed_role_ids": [222222222222222222, 333333333333333333],
    "discord_silent_notifications": true,
    "reddit_client_id": "app_client_id",
    "reddit_client_secret": "app_client_secret",
    "reddit_refresh_token": "refresh_token_here",
    "reddit_user_agent": "reddit-mod-from-discord/0.1 by u/mod_account",
    "reddit_subreddit": "example_subreddit",
    "poll_interval_minutes": 5,
    "post_report_threshold": 1,
    "comment_report_threshold": 1,
    "max_reports_per_poll": 100,
    "max_item_age_hours": 72
  },
  "other_subreddit": {
    "discord_guild_id": 987654321098765432,
    "discord_mod_channel_id": 444444444444444444,
    "discord_allowed_role_ids": [555555555555555555],
    "reddit_client_id": "other_client_id",
    "reddit_client_secret": "other_client_secret",
    "reddit_refresh_token": "other_refresh_token",
    "reddit_user_agent": "reddit-mod-from-discord/0.1 by u/other_mod_account",
    "reddit_subreddit": "other_subreddit",
    "poll_interval_minutes": 2,
    "post_report_threshold": 2,
    "comment_report_threshold": 2,
    "max_reports_per_poll": 50,
    "max_item_age_hours": 72
  }
}
```
