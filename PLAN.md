# Reddit <-> Discord Mod Bot Plan

## Confirmed Requirements

- [x] Language/runtime: Python with pip + `requirements.txt` workflow.
- [x] Single-server setup (no multi-server config required).
- [x] Target subreddit default: `codelyoko` (overridable via `.env`).
- [x] Discord alert destination default channel id: `604768963741876255`.
- [x] Polling model (no streaming): default every 5 minutes.
- [x] Trigger: reported posts/comments at threshold (default 1 for both).
- [x] One Discord message per Reddit queue item (threads optional/manual by mods).
- [x] Persistent Discord views across restarts.
- [x] Actions should support long text inputs for modmail/removal messaging.
- [x] Reddit auth mode: OAuth refresh token.
- [x] Workflow includes "Mark handled" action.
- [x] Removal reply preference: `public_as_subreddit`.
- [x] Discord permissions via allowlisted mod roles.
- [x] Initial allowlisted role ids:
  - [x] `1221785711922122792`
  - [x] `604756836847059015`

## UX Goals

- [x] Keep mod channel signal high and avoid noisy repeat posts.
- [x] Keep each alert actionable with clear buttons + links.
- [x] Provide enough context in embed to act without opening Reddit in many cases.
- [x] Preserve audit trail by updating alert message with action history.

## Technical Design

### Core Bot

- [x] `discord.py` client with slash commands + persistent button views.
- [x] Background polling task (`POLL_INTERVAL_MINUTES`, default 5).
- [x] Role-based permission check against allowlist.

### Reddit Integration

- [x] PRAW client configured via refresh token.
- [x] Poll `subreddit.mod.reports()` and normalize queue items.
- [x] Apply report thresholds (`POST_REPORT_THRESHOLD`, `COMMENT_REPORT_THRESHOLD`).
- [x] Safe wrappers for moderation actions (approve/remove/spam/lock/ban/modmail).

### Persistence (SQLite)

- [x] Table for dedupe records keyed by Reddit fullname (`t3_xxx` / `t1_xxx`).
- [x] Table for persistent view payloads and message mapping.
- [x] Table for lightweight action log / handled status.
- [x] Startup restore + stale record pruning.

### Discord Alert UI

- [x] Embed with item metadata, report count, author, links, snippet.
- [ ] Buttons:
  - [x] Open on Reddit (link)
  - [x] Approve
  - [x] Remove
  - [x] Spam
  - [x] Lock / Unlock
  - [x] Ignore Reports / Unignore Reports
  - [x] Ban (modal)
  - [x] Removal Message (modal)
  - [x] Modmail (modal)
  - [x] Mark Handled
- [x] Modals for long text input:
  - [x] Ban reason + duration fields
  - [x] Removal message body/title fields
  - [x] Modmail subject/body fields

## Safety and Reliability

- [x] Idempotent action handling for repeated button clicks.
- [x] Graceful error responses in Discord ephemerals.
- [ ] Basic retry/backoff handling around Reddit API failures.
- [x] Structured logging with enough detail for troubleshooting.

## Project Setup Files

- [x] `requirements.in` and pinned `requirements.txt`.
- [x] `.env.example` with required settings and sane defaults.
- [x] `Makefile` with install/run/test helper targets.
- [x] `README.md` quick start and operations notes.

## Build Sequence

- [x] Step 1: Scaffold package + dependencies.
- [x] Step 2: Implement config and DB store.
- [x] Step 3: Implement Reddit client wrappers.
- [x] Step 4: Implement Discord report view + modals.
- [x] Step 5: Implement polling loop and message posting.
- [x] Step 6: Implement view restore and handled state.
- [x] Step 7: Smoke-test paths and update docs.
