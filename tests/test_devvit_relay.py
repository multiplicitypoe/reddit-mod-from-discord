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


def _runtime(setup_id, subreddit, channel_id=1):
    settings = SimpleNamespace(discord_mod_channel_id=channel_id, reddit_subreddit=subreddit)
    return SimpleNamespace(setup_id=setup_id, settings=settings)


def _incoming(subreddit, channel_id=1):
    embed = SimpleNamespace(footer=SimpleNamespace(text=f"rmfd:v1 t3_abc r/{subreddit}"))
    return SimpleNamespace(
        id=500, webhook_id=9, embeds=[embed], guild=SimpleNamespace(id=7),
        channel=SimpleNamespace(id=channel_id), delete=AsyncMock(),
    )


def _router(runtimes):
    bot = object.__new__(RedditModBot)
    bot._get_runtimes_for_guild = lambda guild_id: runtimes
    bot._relay_devvit_alert = AsyncMock()
    return bot


def test_subreddits_sharing_a_channel_are_told_apart_by_the_footer():
    first, second = _runtime("a", "examplesub"), _runtime("b", "ExampleSub2")
    bot = _router([first, second])
    asyncio.run(bot.on_message(_incoming("examplesub2")))
    bot._relay_devvit_alert.assert_awaited_once()
    assert bot._relay_devvit_alert.await_args.args[1] is second


def test_an_alert_for_another_channel_or_subreddit_is_left_alone():
    bot = _router([_runtime("a", "examplesub", channel_id=1)])
    asyncio.run(bot.on_message(_incoming("examplesub", channel_id=2)))
    asyncio.run(bot.on_message(_incoming("elsewhere", channel_id=1)))
    bot._relay_devvit_alert.assert_not_awaited()
