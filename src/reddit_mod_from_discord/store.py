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
                setup_id TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                fullname TEXT NOT NULL,
                thing_kind TEXT NOT NULL,
                subreddit TEXT NOT NULL,
                first_reported_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                report_count INTEGER NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0,
                discord_channel_id INTEGER,
                discord_message_id INTEGER,
                PRIMARY KEY (setup_id, fullname)
            );

            CREATE TABLE IF NOT EXISTS alert_views (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS modlog_entries (
                setup_id TEXT NOT NULL,
                fullname TEXT NOT NULL,
                created_utc REAL NOT NULL,
                line TEXT NOT NULL,
                PRIMARY KEY (setup_id, fullname, created_utc, line)
            );

            CREATE INDEX IF NOT EXISTS modlog_entries_lookup
            ON modlog_entries (setup_id, fullname, created_utc);

            CREATE TABLE IF NOT EXISTS modlog_state (
                setup_id TEXT PRIMARY KEY,
                last_seen_utc REAL NOT NULL
            );
            """
        )
        await conn.commit()

        cursor = await conn.execute("PRAGMA table_info(reported_items)")
        columns = [row["name"] for row in await cursor.fetchall()]
        await cursor.close()
        if columns and "setup_id" not in columns:
            has_guild_id = "guild_id" in columns
            await conn.executescript(
                """
                CREATE TABLE reported_items_new (
                    setup_id TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    fullname TEXT NOT NULL,
                    thing_kind TEXT NOT NULL,
                    subreddit TEXT NOT NULL,
                    first_reported_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    report_count INTEGER NOT NULL,
                    handled INTEGER NOT NULL DEFAULT 0,
                    discord_channel_id INTEGER,
                    discord_message_id INTEGER,
                    PRIMARY KEY (setup_id, fullname)
                );
                """
            )
            if has_guild_id:
                await conn.execute(
                    """
                    INSERT INTO reported_items_new (
                        setup_id,
                        guild_id,
                        fullname,
                        thing_kind,
                        subreddit,
                        first_reported_at,
                        last_seen_at,
                        report_count,
                        handled,
                        discord_channel_id,
                        discord_message_id
                    )
                    SELECT
                        CAST(COALESCE(av.guild_id, ri.guild_id, 0) AS TEXT) AS setup_id,
                        COALESCE(av.guild_id, ri.guild_id, 0) AS guild_id,
                        ri.fullname,
                        ri.thing_kind,
                        ri.subreddit,
                        ri.first_reported_at,
                        ri.last_seen_at,
                        ri.report_count,
                        ri.handled,
                        ri.discord_channel_id,
                        ri.discord_message_id
                    FROM reported_items AS ri
                    LEFT JOIN alert_views AS av
                        ON av.message_id = ri.discord_message_id
                    """
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO reported_items_new (
                        setup_id,
                        guild_id,
                        fullname,
                        thing_kind,
                        subreddit,
                        first_reported_at,
                        last_seen_at,
                        report_count,
                        handled,
                        discord_channel_id,
                        discord_message_id
                    )
                    SELECT
                        CAST(COALESCE(av.guild_id, 0) AS TEXT) AS setup_id,
                        COALESCE(av.guild_id, 0) AS guild_id,
                        ri.fullname,
                        ri.thing_kind,
                        ri.subreddit,
                        ri.first_reported_at,
                        ri.last_seen_at,
                        ri.report_count,
                        ri.handled,
                        ri.discord_channel_id,
                        ri.discord_message_id
                    FROM reported_items AS ri
                    LEFT JOIN alert_views AS av
                        ON av.message_id = ri.discord_message_id
                    """
                )
            await conn.executescript(
                """
                DROP TABLE reported_items;
                ALTER TABLE reported_items_new RENAME TO reported_items;
                """
            )
            await conn.commit()

    async def should_alert(self, item: ReportedItem, setup_id: str, guild_id: int) -> bool:
        conn = self._require_conn()
        now = time.time()
        cursor = await conn.execute(
            """
            SELECT discord_message_id, handled
            FROM reported_items
            WHERE setup_id = ? AND fullname = ?
            """,
            (setup_id, item.fullname),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            await conn.execute(
                """
                INSERT INTO reported_items (
                    setup_id,
                    guild_id,
                    fullname,
                    thing_kind,
                    subreddit,
                    first_reported_at,
                    last_seen_at,
                    report_count,
                    handled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    setup_id,
                    guild_id,
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
            WHERE setup_id = ? AND fullname = ?
            """,
            (now, item.num_reports, setup_id, item.fullname),
        )
        await conn.commit()

        message_id = row["discord_message_id"]
        handled = bool(row["handled"])
        if message_id is None:
            return True
        if handled:
            return False
        return False

    async def get_view(self, message_id: int) -> ViewRecord | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT message_id, channel_id, guild_id, payload_json, created_at FROM alert_views WHERE message_id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return ViewRecord(
            message_id=row["message_id"],
            channel_id=row["channel_id"],
            guild_id=row["guild_id"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    async def list_unhandled_alerts(
        self, setup_id: str, limit: int = 50
    ) -> list[tuple[str, int, int]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT ri.fullname, ri.discord_channel_id, ri.discord_message_id
            FROM reported_items AS ri
            INNER JOIN alert_views AS av
                ON av.message_id = ri.discord_message_id
            WHERE
                ri.setup_id = ?
                AND ri.handled = 0
                AND ri.discord_message_id IS NOT NULL
                AND ri.discord_channel_id IS NOT NULL
            ORDER BY ri.last_seen_at DESC
            LIMIT ?
            """,
            (setup_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        out: list[tuple[str, int, int]] = []
        for row in rows:
            out.append((row["fullname"], int(row["discord_channel_id"]), int(row["discord_message_id"])))
        return out

    async def set_discord_message(
        self, fullname: str, setup_id: str, channel_id: int, message_id: int
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE reported_items
            SET discord_channel_id = ?, discord_message_id = ?, handled = 0
            WHERE setup_id = ? AND fullname = ?
            """,
            (channel_id, message_id, setup_id, fullname),
        )
        await conn.commit()

    async def clear_discord_message(self, fullname: str, setup_id: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE reported_items
            SET discord_channel_id = NULL, discord_message_id = NULL
            WHERE setup_id = ? AND fullname = ?
            """,
            (setup_id, fullname),
        )
        await conn.commit()

    async def get_alert_message(
        self, fullname: str, setup_id: str
    ) -> tuple[int | None, int | None, bool]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT discord_channel_id, discord_message_id, handled
            FROM reported_items
            WHERE setup_id = ? AND fullname = ?
            """,
            (setup_id, fullname),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None, None, False
        channel_id = row["discord_channel_id"]
        message_id = row["discord_message_id"]
        handled = bool(row["handled"])
        return (
            int(channel_id) if channel_id is not None else None,
            int(message_id) if message_id is not None else None,
            handled,
        )

    async def mark_handled(self, fullname: str, setup_id: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "UPDATE reported_items SET handled = 1 WHERE setup_id = ? AND fullname = ?",
            (setup_id, fullname),
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

    async def get_modlog_state(self, setup_id: str) -> float | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT last_seen_utc FROM modlog_state WHERE setup_id = ?",
            (setup_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return float(row["last_seen_utc"])

    async def update_modlog_state(self, setup_id: str, last_seen_utc: float) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO modlog_state (setup_id, last_seen_utc)
            VALUES (?, ?)
            ON CONFLICT(setup_id) DO UPDATE SET last_seen_utc = excluded.last_seen_utc
            """,
            (setup_id, float(last_seen_utc)),
        )
        await conn.commit()

    async def save_modlog_entries(
        self, setup_id: str, entries: list[tuple[str, float, str]]
    ) -> None:
        if not entries:
            return
        conn = self._require_conn()
        await conn.executemany(
            """
            INSERT OR IGNORE INTO modlog_entries (setup_id, fullname, created_utc, line)
            VALUES (?, ?, ?, ?)
            """,
            [(setup_id, fullname, created_utc, line) for fullname, created_utc, line in entries],
        )
        await conn.commit()

    async def list_modlog_entries(
        self,
        setup_id: str,
        fullname: str,
        *,
        max_age_s: float | None = None,
        limit: int = 50,
    ) -> list[str]:
        conn = self._require_conn()
        params: list[object] = [setup_id, fullname]
        where = "setup_id = ? AND fullname = ?"
        if max_age_s is not None:
            cutoff = time.time() - max_age_s
            where += " AND created_utc >= ?"
            params.append(cutoff)
        params.append(limit)
        cursor = await conn.execute(
            f"""
            SELECT line
            FROM modlog_entries
            WHERE {where}
            ORDER BY created_utc DESC
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        lines = [str(row["line"]) for row in rows]
        lines.reverse()
        return lines

    async def prune_modlog_entries(self, setup_id: str, max_age_s: float) -> None:
        if max_age_s <= 0:
            return
        cutoff = time.time() - max_age_s
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM modlog_entries WHERE setup_id = ? AND created_utc < ?",
            (setup_id, cutoff),
        )
        await conn.commit()

    async def clear_setup_history(self, setup_id: str) -> None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT DISTINCT discord_message_id FROM reported_items WHERE setup_id = ?",
            (setup_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        message_ids = [row["discord_message_id"] for row in rows if row["discord_message_id"]]
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            await conn.execute(
                f"DELETE FROM alert_views WHERE message_id IN ({placeholders})",
                tuple(message_ids),
            )
        await conn.execute("DELETE FROM reported_items WHERE setup_id = ?", (setup_id,))
        await conn.execute("DELETE FROM modlog_entries WHERE setup_id = ?", (setup_id,))
        await conn.execute("DELETE FROM modlog_state WHERE setup_id = ?", (setup_id,))
        await conn.commit()
