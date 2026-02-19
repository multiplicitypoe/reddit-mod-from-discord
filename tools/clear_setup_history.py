from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from reddit_mod_from_discord.config import load_settings
from reddit_mod_from_discord.store import BotStore


def _resolve_setup_id(settings) -> str:
    setup_id = (os.getenv("CLEAR_SETUP_ID") or "").strip()
    if settings.multi_server_config:
        if setup_id:
            if setup_id not in settings.multi_server_config:
                raise SystemExit(f"Unknown CLEAR_SETUP_ID: {setup_id}")
            return setup_id
        if len(settings.multi_server_config) != 1:
            raise SystemExit(
                "CLEAR_SETUP_ID is required when MULTI_SERVER_CONFIG_PATH has multiple setups"
            )
        return next(iter(settings.multi_server_config.keys()))
    if not setup_id:
        raise SystemExit("CLEAR_SETUP_ID is required for single-setup mode")
    return setup_id


async def _run() -> None:
    settings = load_settings()
    setup_id = _resolve_setup_id(settings)
    store = BotStore(settings.db_path)
    await store.connect()
    try:
        await store.clear_setup_history(setup_id)
    finally:
        await store.close()
    print(f"Cleared history for setup: {setup_id}")


def main() -> None:
    load_dotenv()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
