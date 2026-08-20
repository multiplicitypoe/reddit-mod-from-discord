"""The background half of a button press has to survive being started.

Every button here answers straight away and finishes the job afterwards. A task
whose only reference is a local variable can be collected part way through,
because the event loop keeps only weak references to tasks, and the cancellation
arrives as a BaseException so the done callback returns early on t.cancelled()
without logging. The same shape in the incident assistant meant its audit log
summary never once reached a card, in total silence.
"""
import asyncio
import gc
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.discord_ui.report_view import (  # noqa: E402
    _BACKGROUND_TASKS,
    _spawn,
)


def test_background_work_is_held_until_it_finishes():
    """Every button here answers fast and finishes the job in the background.

    A task whose only reference is a local variable can be collected part way
    through, because the event loop keeps only weak references to tasks. The
    same shape in the incident assistant meant its audit log summary never once
    appeared, and it failed in silence: the cancellation arrives as a
    BaseException and the done callback returns early on t.cancelled().
    """
    finished = []

    async def body():
        async def work():
            await asyncio.sleep(0.01)
            finished.append(True)

        task = _spawn(work(), "test")
        assert task in _BACKGROUND_TASKS
        del task
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(body())
    assert finished == [True], "the task was collected before it could finish"
    assert not _BACKGROUND_TASKS, "finished tasks should be let go of"
