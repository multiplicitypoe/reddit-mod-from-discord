# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV TZ=UTC
ENV HOME=/tmp
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_ONLY_BINARY=:all:
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN set -eux; \
    extra=""; \
    if [ "$(dpkg --print-architecture)" = "armhf" ]; then \
        extra="--extra-index-url https://www.piwheels.org/simple"; \
    fi; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir --only-binary=:all: $extra -r /app/requirements.txt

COPY src /app/src

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

ENV PYTHONPATH=/app/src

USER app

CMD ["python", "-m", "reddit_mod_from_discord"]
