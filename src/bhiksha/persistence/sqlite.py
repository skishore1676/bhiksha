"""SQLite-backed persistence for runtime events."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.persistence.repository import EventRepository


class SQLiteEventRepository(EventRepository):
    """Append-only event log stored in SQLite."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._initialized:
            await asyncio.to_thread(self._init_db)
            self._initialized = True
        await asyncio.to_thread(self._append_sync, event_type, payload)

    def _init_db(self) -> None:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _append_sync(self, event_type: str, payload: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO events (created_at, event_type, payload) VALUES (?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    event_type,
                    json.dumps(payload, default=str),
                ),
            )
            conn.commit()

