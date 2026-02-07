from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from reddit_mod_from_discord.models import ReportedItem


@dataclass
class ViewRecord:
    message_id: int
    channel_id: int
    guild_id: int
    payload: dict[str, Any]
    created_at: float


class BotStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._ensure_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("BotStore is not connected")
        return self._conn

    async def _ensure_schema(self) -> None:
        conn = self._require_conn()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reported_items (
                fullname TEXT PRIMARY KEY,
                thing_kind TEXT NOT NULL,
                subreddit TEXT NOT NULL,
                first_reported_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                report_count INTEGER NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0,
                discord_channel_id INTEGER,
                discord_message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS alert_views (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        await conn.commit()

    async def should_alert(self, item: ReportedItem) -> bool:
        conn = self._require_conn()
        now = time.time()
        cursor = await conn.execute(
            "SELECT discord_message_id, handled FROM reported_items WHERE fullname = ?",
            (item.fullname,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            await conn.execute(
                """
                INSERT INTO reported_items (
                    fullname,
                    thing_kind,
                    subreddit,
                    first_reported_at,
                    last_seen_at,
                    report_count,
                    handled
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    item.fullname,
                    item.kind,
                    item.subreddit,
                    now,
                    now,
                    item.num_reports,
                ),
            )
            await conn.commit()
            return True

        await conn.execute(
            """
            UPDATE reported_items
            SET last_seen_at = ?, report_count = ?
            WHERE fullname = ?
            """,
            (now, item.num_reports, item.fullname),
        )
        await conn.commit()

        message_id = row["discord_message_id"]
        handled = bool(row["handled"])
        if message_id is None:
            return True
        if handled:
            return False
        return False

    async def set_discord_message(self, fullname: str, channel_id: int, message_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE reported_items
            SET discord_channel_id = ?, discord_message_id = ?, handled = 0
            WHERE fullname = ?
            """,
            (channel_id, message_id, fullname),
        )
        await conn.commit()

    async def mark_handled(self, fullname: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "UPDATE reported_items SET handled = 1 WHERE fullname = ?",
            (fullname,),
        )
        await conn.commit()

    async def save_view(self, record: ViewRecord) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO alert_views (message_id, channel_id, guild_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                record.message_id,
                record.channel_id,
                record.guild_id,
                json.dumps(record.payload, ensure_ascii=True),
                record.created_at,
            ),
        )
        await conn.commit()

    async def load_views(self) -> list[ViewRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT message_id, channel_id, guild_id, payload_json, created_at FROM alert_views"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records: list[ViewRecord] = []
        for row in rows:
            records.append(
                ViewRecord(
                    message_id=row["message_id"],
                    channel_id=row["channel_id"],
                    guild_id=row["guild_id"],
                    payload=json.loads(row["payload_json"]),
                    created_at=row["created_at"],
                )
            )
        return records

    async def delete_view(self, message_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM alert_views WHERE message_id = ?", (message_id,))
        await conn.commit()

    async def prune_views(self, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        cutoff = time.time() - ttl_s
        conn = self._require_conn()
        await conn.execute("DELETE FROM alert_views WHERE created_at < ?", (cutoff,))
        await conn.commit()
