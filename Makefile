.PHONY: ensure-venv install run-bot demo-mode test-reddit test-discord test-discord-post test-discord-comment reddit-token ensure-data ensure-env build-docker stop-docker run-docker docker run-docker-bot help

VENV ?= .venv
PYTHON ?= python3
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
PIP_INSTALL_FLAGS ?= --no-cache-dir --only-binary=:all:

DOCKER_BIN ?= docker
DOCKER ?= $(shell \
	if command -v "$(DOCKER_BIN)" >/dev/null 2>&1; then \
		if "$(DOCKER_BIN)" ps >/dev/null 2>&1; then \
			printf '%s' "$(DOCKER_BIN)"; \
		elif command -v sudo >/dev/null 2>&1 && sudo -n "$(DOCKER_BIN)" ps >/dev/null 2>&1; then \
			printf '%s' "sudo -n $(DOCKER_BIN)"; \
		else \
			printf '%s' "$(DOCKER_BIN)"; \
		fi; \
	else \
		printf '%s' "$(DOCKER_BIN)"; \
	fi)
IMAGE ?= reddit-mod-from-discord
TAG ?= latest
CONTAINER ?= reddit-mod-from-discord

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

ensure-data:
	@mkdir -p data

ensure-env:
	@test -f "$(CURDIR)/.env" || (cp "$(CURDIR)/.env.example" "$(CURDIR)/.env" && \
		printf '%s\n' "Created .env from .env.example. Edit .env, then re-run." && \
		exit 1)

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

build-docker: ensure-data
	$(DOCKER) build -t "$(IMAGE):$(TAG)" "$(CURDIR)"

stop-docker:
	@if $(DOCKER) container inspect "$(CONTAINER)" >/dev/null 2>&1; then \
		if [ "$$($(DOCKER) inspect -f '{{.State.Running}}' "$(CONTAINER)" 2>/dev/null)" = "true" ]; then \
			echo "Stopping container $(CONTAINER)"; \
			$(DOCKER) stop "$(CONTAINER)" >/dev/null; \
		fi; \
		echo "Removing container $(CONTAINER)"; \
		$(DOCKER) rm "$(CONTAINER)" >/dev/null; \
	else \
		echo "Container $(CONTAINER) not found; nothing to stop."; \
	fi

run-docker: ensure-env ensure-data stop-docker
	@config_mount=""; \
	if [ -f "$(CURDIR)/multi_server_config.json" ]; then \
		config_mount="-v $(CURDIR)/multi_server_config.json:/app/multi_server_config.json:ro"; \
	fi; \
	$(DOCKER) run --rm \
		--name "$(CONTAINER)" \
		--env-file "$(CURDIR)/.env" \
		-v "$(CURDIR)/data:/app/data" \
		$$config_mount \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,nodev \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--pids-limit 256 \
		--memory 512m \
		--cpus 1.0 \
		--user "$$(id -u):$$(id -g)" \
		"$(IMAGE):$(TAG)"

docker: build-docker run-docker

run-docker-bot: run-docker
