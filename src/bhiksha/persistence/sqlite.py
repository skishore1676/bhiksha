"""SQLite-backed persistence for runtime events."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.domain.models import TradeRecord
from bhiksha.persistence.repository import EventRepository
from bhiksha.persistence.repository import TradeStateRepository


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


class SQLiteTradeStateRepository(TradeStateRepository):
    """SQLite-backed durable trade session store."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    async def upsert_trade(self, record: TradeRecord) -> None:
        if not self._initialized:
            await asyncio.to_thread(self._init_db)
            self._initialized = True
        await asyncio.to_thread(self._upsert_trade_sync, record)

    async def mark_closed(self, trade_id: str, *, exit_order_id: str | None = None) -> None:
        if not self._initialized:
            await asyncio.to_thread(self._init_db)
            self._initialized = True
        await asyncio.to_thread(self._mark_closed_sync, trade_id, exit_order_id)

    async def get_open_trades(self) -> list[TradeRecord]:
        if not self._initialized:
            await asyncio.to_thread(self._init_db)
            self._initialized = True
        return await asyncio.to_thread(self._get_open_trades_sync)

    def _init_db(self) -> None:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_sessions (
                    trade_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    option_symbol TEXT,
                    quantity INTEGER NOT NULL,
                    entry_price REAL,
                    status TEXT NOT NULL,
                    entry_order_id TEXT,
                    stop_order_id TEXT,
                    stop_price REAL,
                    target_order_id TEXT,
                    target_price REAL,
                    exit_order_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _upsert_trade_sync(self, record: TradeRecord) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trade_sessions (
                    trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, status,
                    entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                    exit_order_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    deployment_id=excluded.deployment_id,
                    symbol=excluded.symbol,
                    option_symbol=excluded.option_symbol,
                    quantity=excluded.quantity,
                    entry_price=excluded.entry_price,
                    status=excluded.status,
                    entry_order_id=excluded.entry_order_id,
                    stop_order_id=excluded.stop_order_id,
                    stop_price=excluded.stop_price,
                    target_order_id=excluded.target_order_id,
                    target_price=excluded.target_price,
                    exit_order_id=excluded.exit_order_id,
                    updated_at=excluded.updated_at
                """,
                (
                    record.trade_id,
                    record.deployment_id,
                    record.symbol,
                    record.option_symbol,
                    record.quantity,
                    record.entry_price,
                    record.status,
                    record.entry_order_id,
                    record.stop_order_id,
                    record.stop_price,
                    record.target_order_id,
                    record.target_price,
                    record.exit_order_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def _mark_closed_sync(self, trade_id: str, exit_order_id: str | None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE trade_sessions
                SET status = ?, exit_order_id = COALESCE(?, exit_order_id), updated_at = ?
                WHERE trade_id = ?
                """,
                ("closed", exit_order_id, datetime.now(UTC).isoformat(), trade_id),
            )
            conn.commit()

    def _get_open_trades_sync(self) -> list[TradeRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, status,
                       entry_order_id, stop_order_id, stop_price, target_order_id, target_price, exit_order_id
                FROM trade_sessions
                WHERE status != 'closed'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            TradeRecord(
                trade_id=row[0],
                deployment_id=row[1],
                symbol=row[2],
                option_symbol=row[3],
                quantity=row[4],
                entry_price=row[5],
                status=row[6],
                entry_order_id=row[7],
                stop_order_id=row[8],
                stop_price=row[9],
                target_order_id=row[10],
                target_price=row[11],
                exit_order_id=row[12],
            )
            for row in rows
        ]
