import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.bot import RedditModBot  # noqa: E402


def _bot(card_message_id):
    bot = object.__new__(RedditModBot)
    bot._poll_once = AsyncMock(return_value=1)
    bot.store = SimpleNamespace(get_alert_message=AsyncMock(return_value=(10, card_message_id, False)))
    return bot


def _message():
    return SimpleNamespace(id=500, guild=SimpleNamespace(id=7), delete=AsyncMock())


def _relay(bot, message):
    runtime = SimpleNamespace(setup_id="s")
    asyncio.run(bot._relay_devvit_alert(message, runtime, "t3_abc"))


def test_the_webhook_message_is_deleted_once_the_card_exists():
    bot, message = _bot(card_message_id=900), _message()
    _relay(bot, message)
    bot._poll_once.assert_awaited_once()
    message.delete.assert_awaited_once()


def test_the_webhook_message_stays_when_no_card_was_made():
    bot, message = _bot(card_message_id=None), _message()
    _relay(bot, message)
    message.delete.assert_not_awaited()


def test_the_webhook_message_stays_when_polling_fails():
    bot, message = _bot(card_message_id=900), _message()
    bot._poll_once = AsyncMock(side_effect=RuntimeError("reddit is down"))
    _relay(bot, message)
    message.delete.assert_not_awaited()
