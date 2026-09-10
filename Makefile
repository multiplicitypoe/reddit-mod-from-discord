# reddit-mod-from-discord: mirrors subreddit mod activity into Discord.

VENV ?= .venv
PYTHON ?= python3
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
PIP_INSTALL_FLAGS ?= --no-cache-dir --only-binary=:all:

IMAGE ?= reddit-mod-from-discord
CONTAINER ?= reddit-mod-from-discord
DATA_DIR ?= data

RUN_ARGS = \
	--env-file "$(CURDIR)/.env" \
	-v "$(CURDIR)/data:/app/data"
# Mounted only when the file is there, which is how it has always behaved.
ifneq ($(wildcard $(CURDIR)/multi_server_config.json),)
RUN_ARGS += -v "$(CURDIR)/multi_server_config.json:/app/multi_server_config.json:ro"
endif
RUN_ARGS += $(HARDENED_ARGS)

include docker.mk

.PHONY: help ensure-venv install run-bot demo-mode test-reddit test-discord \
        test-discord-post test-discord-comment reddit-token clear-history

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
	@echo "make build-docker - build Docker image ($(IMAGE):$(TAG))"
	@echo "make stop-docker  - stop/remove current Docker container ($(CONTAINER))"
	@echo "make run-docker   - restart named Docker container in foreground"
	@echo "make docker       - build image then run container"

ensure-venv:
	@test -x "$(PY)" || ($(PYTHON) -m venv "$(VENV)" && "$(PIP)" install --upgrade pip)

install: ensure-venv
	"$(PIP)" install $(PIP_INSTALL_FLAGS) -r requirements.txt

run-bot: install ensure-env
	PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord

demo-mode: install ensure-env
	DEMO_MODE=true DB_PATH=data/reddit_mod_from_discord_demo.sqlite3 PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord

test-reddit: install ensure-env
	PYTHONPATH=src "$(PY)" -m reddit_mod_from_discord.reddit_client

test-discord: test-discord-post

test-discord-post: install ensure-env
	TEST_KIND=submission PYTHONPATH=src "$(PY)" tools/send_test_discord_alert.py

test-discord-comment: install ensure-env
	TEST_KIND=comment PYTHONPATH=src "$(PY)" tools/send_test_discord_alert.py

reddit-token: install ensure-env
	PYTHONPATH=src "$(PY)" tools/obtain_refresh_token.py

clear-history: install ensure-env
	PYTHONPATH=src "$(PY)" tools/clear_setup_history.py
