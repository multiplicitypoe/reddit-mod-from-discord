# Reddit Mod from Discord

Discord bot that polls Reddit reports for a single subreddit and posts one actionable alert message per reported item into a Discord mod channel.

## What this does

- Polls `r/{subreddit}` report queue on an interval (default 5 minutes)
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

Defaults already set in `.env.example`:

- `DISCORD_SILENT_NOTIFICATIONS=true`

Note: `DISCORD_ALLOWED_ROLE_IDS` are server-specific. If you move the bot to a new Discord server, you must update this list to the new server's role IDs.
- `REDDIT_SUBREDDIT=`
- `POLL_INTERVAL_MINUTES=5`
- `POST_REPORT_THRESHOLD=1`
- `COMMENT_REPORT_THRESHOLD=1`
- `MAX_REPORTS_PER_POLL=100`
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
