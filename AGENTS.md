# Agent & Contributor Guidelines

## Privacy: no real identities in anything committed

This bot moderates real Discord communities and handles real Reddit report
data — real usernames, real message content, real subreddit names. **None
of that may ever appear in a commit, pull request, test fixture, code
comment, or example in this repository.** This applies equally to code
written by hand and code written by an AI coding agent.

Specifically, never commit:

- **Real Discord usernames, display names, or user IDs** belonging to
  actual server members or moderators — in code, comments, commit
  messages, or PR descriptions.
- **Real Reddit usernames**, reported or otherwise.
- **Real subreddit names** in example data, test fixtures, or docs. This
  is not hypothetical: a demo-post URL default in this codebase named the
  actual community in its post slug even after the subreddit segment of
  the same URL had already been anonymized to `r/example` - the real name
  just moved to a different part of the same string. Check the *whole*
  string, not just the obvious part.
- **Real people's names**, including moderators, contributors, or anyone
  mentioned in an issue or incident this code happens to address.
- Real message or report content pulled from an actual server, even
  partially redacted — write synthetic examples instead.

If you need example data for a test, a demo, or a docstring, invent it. A
fake subreddit (`r/examplesub`), a fake username (`some_user_123`), and
fake comment text cost nothing and carry zero risk. There is no situation
where using a real one is actually necessary.

If you're an AI agent working in this repo: this rule applies to every
artifact you produce here — source code, test fixtures, commit messages,
PR titles and descriptions, and code comments alike. Before committing,
re-read what you just wrote and check it for anything that identifies a
real person or a real community by name.

## Working in this repo

- Runtime: Python, `discord.py` + `praw`. See `README.md` for setup.
- Tests: `pytest tests` — keep the suite passing before committing.
- The bot runs in a hardened Docker container (`--read-only`,
  `--cap-drop ALL`, no writable filesystem outside `/app/data` and a
  `noexec` `/tmp`). If you add a dependency that needs system libraries or
  writes to disk, verify it actually works under those constraints before
  assuming a rebuild will work in production — several image-rendering
  libraries needed extra shared libraries added to the `Dockerfile` before
  they'd import at all under this setup.
