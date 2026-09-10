# Shared Docker lifecycle for the bots.
#
# This file is vendored identically into every bot repo. Change it in one place
# and copy it out; do not let the copies drift.
#
# A project supplies, before including this:
#
#   IMAGE        image name           (default: the directory name)
#   CONTAINER    container name       (default: IMAGE)
#   TAG          image tag            (default: latest)
#   RUN_ARGS     extra `docker run` arguments: mounts, env-file, limits
#   PRE_RUN      a shell command to run just before the container starts
#   DATA_DIR     directories to create before running, space separated
#   ENV_HINT     extra words for the "edit .env" message, e.g. which keys.
#                Give it bare text, no quotes: it is interpolated inside a
#                double quoted shell string.
#
# and may opt into the shared hardening profile with:
#
#   RUN_ARGS += $(HARDENED_ARGS)
#
# and gets: build-docker, stop-docker, run-docker, docker, ensure-data,
# ensure-env, print-container.

.PHONY: build-docker stop-docker run-docker run-docker-bot docker ensure-data ensure-env print-container

DOCKER_BIN ?= docker
# Prefer docker directly, fall back to passwordless sudo. The bots run as a user
# who is deliberately not in the docker group.
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

IMAGE ?= $(notdir $(CURDIR))
CONTAINER ?= $(IMAGE)
TAG ?= latest
RUN_ARGS ?=
PRE_RUN ?= true
DATA_DIR ?=
ENV_HINT ?=

# The hardening every bot that touches the network should be running under.
# Opt in with `RUN_ARGS += $(HARDENED_ARGS)` rather than copying the flags.
HARDENED_ARGS ?= \
	--read-only \
	--tmpfs /tmp:rw,noexec,nosuid,nodev \
	--cap-drop ALL \
	--security-opt no-new-privileges \
	--pids-limit 256 \
	--memory 512m \
	--cpus 1.0 \
	--user "$$(id -u):$$(id -g)"
STOP_TIMEOUT ?= 10

# So a supervisor wrapper, or a person, can ask what this project calls its
# container without parsing the Makefile.
print-container:
	@echo $(CONTAINER)

# Only projects that keep state on disk declare a DATA_DIR.
ensure-data:
	@if [ -n "$(DATA_DIR)" ]; then mkdir -p $(DATA_DIR); fi

# Only projects that ship a .env.example need a .env.
ensure-env:
	@if [ -f "$(CURDIR)/.env.example" ] && [ ! -f "$(CURDIR)/.env" ]; then \
		cp "$(CURDIR)/.env.example" "$(CURDIR)/.env"; \
		printf '%s\n' "Created .env from .env.example. Edit .env$(if $(ENV_HINT), $(ENV_HINT)), then re-run."; \
		exit 1; \
	fi

build-docker: ensure-data
	$(DOCKER) build -t "$(IMAGE):$(TAG)" "$(CURDIR)"

stop-docker:
	@if $(DOCKER) container inspect "$(CONTAINER)" >/dev/null 2>&1; then \
		if [ "$$($(DOCKER) inspect -f '{{.State.Running}}' "$(CONTAINER)" 2>/dev/null)" = "true" ]; then \
			printf '%s\n' "Stopping container $(CONTAINER)"; \
			$(DOCKER) stop -t $(STOP_TIMEOUT) "$(CONTAINER)" >/dev/null; \
		fi; \
		printf '%s\n' "Removing container $(CONTAINER)"; \
		$(DOCKER) rm "$(CONTAINER)" >/dev/null 2>&1 || true; \
	else \
		printf '%s\n' "Container $(CONTAINER) not found; nothing to stop."; \
	fi

# The container belongs to the docker daemon, not to this shell. Without the
# trap, a signal that kills make leaves the bot running: supervisorctl would
# report the program stopped while it carried on, and the next start would find
# a live container and keep serving the old image.
run-docker: build-docker ensure-env ensure-data stop-docker
	@trap '$(DOCKER) stop -t $(STOP_TIMEOUT) "$(CONTAINER)" >/dev/null 2>&1 || true' INT TERM; \
	$(PRE_RUN); \
	$(DOCKER) run --rm \
		--name "$(CONTAINER)" \
		$(RUN_ARGS) \
		"$(IMAGE):$(TAG)" & \
	wait $$!

docker: run-docker

# Both reddit bots grew this alias independently, so it lives here now.
run-docker-bot: run-docker
