.PHONY: ensure-venv install run-bot demo-mode test-reddit test-discord test-discord-post test-discord-comment reddit-token help

VENV ?= .venv
PYTHON ?= python3
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

help:
	@echo "make ensure-venv - create .venv if missing"
	@echo "make install     - install runtime deps"
	@echo "make run-bot     - run the Discord bot with .env"
	@echo "make demo-mode   - run bot in safe demo mode"
	@echo "make test-reddit - validate Reddit auth/config"
	@echo "make test-discord - send a test alert (post)"
	@echo "make test-discord-post - send a test post alert"
	@echo "make test-discord-comment - send a test comment alert"
	@echo "make reddit-token - obtain Reddit refresh token"

ensure-venv:
	@test -x "$(PY)" || ($(PYTHON) -m venv "$(VENV)" && "$(PIP)" install --upgrade pip)

install: ensure-venv
	"$(PIP)" install --no-cache-dir --only-binary=:all: -r requirements.txt

run-bot: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord

demo-mode: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	DEMO_MODE=true DB_PATH=data/reddit_mod_from_discord_demo.sqlite3 PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord

test-reddit: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord.reddit_client


test-discord: test-discord-post

test-discord-post: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	TEST_KIND=submission PYTHONPATH=src "$(PY)" tools/send_test_discord_alert.py

test-discord-comment: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	TEST_KIND=comment PYTHONPATH=src "$(PY)" tools/send_test_discord_alert.py

reddit-token: install
	@test -f ./.env || (echo "Missing .env. Run: cp .env.example .env" && exit 1)
	PYTHONPATH=src "$(PY)" tools/obtain_refresh_token.py
