from __future__ import annotations

import logging

import discord


class _DiscordConnectionClosed1000ReconnectFilter(logging.Filter):
    """
    Collapse discord.py's noisy stacktrace for normal Gateway close (code 1000).

    discord.py logs these with logger.exception(), which includes a traceback even
    though reconnect + RESUME is expected. Keep a single short line so that
    reconnect frequency is still visible in logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "discord.client":
            return True
        if not record.exc_info:
            return True

        exc = record.exc_info[1]
        if not isinstance(exc, discord.errors.ConnectionClosed):
            return True
        if getattr(exc, "code", None) != 1000:
            return True

        if not isinstance(record.msg, str) or "Attempting a reconnect in" not in record.msg:
            return True

        retry_s: float | None = None
        if isinstance(record.args, tuple) and record.args:
            arg0 = record.args[0]
            if isinstance(arg0, (int, float)):
                retry_s = float(arg0)

        record.exc_info = None
        record.exc_text = None
        record.levelno = logging.WARNING
        record.levelname = logging.getLevelName(logging.WARNING)

        if retry_s is None:
            record.msg = "Discord gateway closed normally (code=1000); reconnecting"
            record.args = ()
        else:
            record.msg = "Discord gateway closed normally (code=1000); reconnecting in %.2fs"
            record.args = (retry_s,)

        return True


def install_discord_reconnect_log_compaction() -> None:
    logging.getLogger("discord.client").addFilter(_DiscordConnectionClosed1000ReconnectFilter())

