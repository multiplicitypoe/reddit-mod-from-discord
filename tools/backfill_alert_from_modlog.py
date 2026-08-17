"""Re-render a handled Reddit alert as the new Mark Handled would have left it.

Folds the item's modlog lines into the audit log and refreshes the status flags,
then rebuilds the embed with the bot's own renderer.

    python retrofit_reddit_alert.py <message_id>            # dry run
    python retrofit_reddit_alert.py <message_id> --apply
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys

import discord

from reddit_mod_from_discord.discord_ui.report_view import build_report_embed
from reddit_mod_from_discord.models import ReportViewPayload

DB = "/app/data/reddit_mod_from_discord.sqlite3"
MESSAGE_ID = int(sys.argv[1])
APPLY = "--apply" in sys.argv


def load():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = c.execute(
        "select payload_json, channel_id, guild_id from alert_views where message_id=?",
        (MESSAGE_ID,),
    ).fetchone()
    if not row:
        raise SystemExit(f"no stored view for {MESSAGE_ID}")
    payload = ReportViewPayload.from_dict(json.loads(row[0]))
    lines = [
        r[0] for r in c.execute(
            "select line from modlog_entries where fullname=? order by created_utc",
            (payload.fullname,),
        )
    ]
    c.close()
    return payload, row[1], row[2], lines


async def main() -> None:
    payload, channel_id, guild_id, modlog_lines = load()
    print(f"  item     : {payload.fullname} in r/{payload.subreddit}")
    print(f"  audit log before: {payload.action_log}")
    print(f"  removed={payload.removed} approved={payload.approved}")

    existing = set(payload.action_log)
    added = [l for l in modlog_lines if l not in existing]
    # keep chronological order: modlog lines first, then what the bot recorded
    if added:
        payload.action_log = added + payload.action_log
    print(f"  modlog lines to fold in: {len(added)}")
    for l in added:
        print(f"    + {l}")
    # the removal is evident from the modlog even without a Reddit round trip
    if any("removecomment" in l or "removelink" in l or "spam" in l for l in added):
        payload.removed = True
        print("  status -> removed=True")

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            message = await channel.fetch_message(MESSAGE_ID)
            embed = build_report_embed(payload)
            if not APPLY:
                print("\n  DRY RUN, embed would render:")
                for f in embed.fields:
                    print(f"    [{f.name}]")
                    for line in str(f.value).splitlines():
                        print(f"      {line}")
                return
            # Leave the view alone: this only restates the audit log.
            await message.edit(embed=embed)
            # Persist too, so a later button press re-renders from the corrected
            # payload rather than putting the old audit log back.
            w = sqlite3.connect(DB)
            w.execute(
                "update alert_views set payload_json=? where message_id=?",
                (json.dumps(payload.to_dict()), MESSAGE_ID),
            )
            w.commit()
            w.close()
            print("\n  APPLIED (message edited, payload persisted)")
        finally:
            await client.close()

    await client.start(os.environ["DISCORD_TOKEN"])


asyncio.run(main())
