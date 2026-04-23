"""SQLite-backed persistence for runtime events."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import CashBudgetDay, CashBudgetReservation, TradeRecord
from bhiksha.persistence.repository import CashBudgetRepository, EventRepository, TradeStateRepository

ReadResultT = TypeVar("ReadResultT")


class SQLiteBackend:
    """Shared SQLite connection policy plus serialized writes for one DB path."""

    def __init__(self, db_path: str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        self._db_initialized = False
        self._db_init_lock = asyncio.Lock()

    async def ensure_db(self) -> None:
        if self._db_initialized:
            return
        async with self._db_init_lock:
            if self._db_initialized:
                return
            await asyncio.to_thread(self._ensure_db_sync)
            self._db_initialized = True

    async def run_write(self, operation: Callable[..., None], *args) -> None:
        await self.ensure_db()
        async with self._write_lock:
            await asyncio.to_thread(operation, *args)

    async def run_read(self, operation: Callable[..., ReadResultT], *args) -> ReadResultT:
        await self.ensure_db()
        return await asyncio.to_thread(operation, *args)

    def connect(self) -> sqlite3.Connection:
        """Open a connection with the runtime SQLite policy applied."""
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000.0)
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_db_sync(self) -> None:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()):
            pass


class SQLiteEventRepository(EventRepository):
    """Append-only event log stored in SQLite."""

    def __init__(self, db_path: str, *, backend: SQLiteBackend | None = None) -> None:
        self.db_path = db_path
        self.backend = backend or SQLiteBackend(db_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._append_sync, event_type, payload)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.backend.run_write(self._init_db)
            self._initialized = True

    def _init_db(self) -> None:
        with closing(self.backend.connect()) as conn:
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
        with closing(self.backend.connect()) as conn:
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

    def __init__(self, db_path: str, *, backend: SQLiteBackend | None = None) -> None:
        self.db_path = db_path
        self.backend = backend or SQLiteBackend(db_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def upsert_trade(self, record: TradeRecord) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._upsert_trade_sync, record)

    async def mark_closed(self, trade_id: str, *, exit_order_id: str | None = None) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._mark_closed_sync, trade_id, exit_order_id)

    async def get_open_trades(self) -> list[TradeRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_open_trades_sync)

    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_recent_trades_sync, limit)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.backend.run_write(self._init_db)
            self._initialized = True

    def _init_db(self) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_sessions (
                    trade_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    option_symbol TEXT,
                    quantity INTEGER NOT NULL,
                    entry_price REAL,
                    underlying_entry_price REAL,
                    entry_timestamp TEXT,
                    status TEXT NOT NULL,
                    entry_order_id TEXT,
                    stop_order_id TEXT,
                    stop_price REAL,
                    target_order_id TEXT,
                    target_price REAL,
                    exit_order_id TEXT,
                    exit_limit_price REAL,
                    exit_submitted_at TEXT,
                    exit_mode TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(trade_sessions)").fetchall()
            }
            if "underlying_entry_price" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN underlying_entry_price REAL")
            if "entry_timestamp" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN entry_timestamp TEXT")
            if "exit_limit_price" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_limit_price REAL")
            if "exit_submitted_at" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_submitted_at TEXT")
            if "exit_mode" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_mode TEXT")
            conn.commit()

    def _upsert_trade_sync(self, record: TradeRecord) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO trade_sessions (
                    trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, underlying_entry_price,
                    entry_timestamp, status, entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                    exit_order_id, exit_limit_price, exit_submitted_at, exit_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    deployment_id=excluded.deployment_id,
                    symbol=excluded.symbol,
                    option_symbol=excluded.option_symbol,
                    quantity=excluded.quantity,
                    entry_price=excluded.entry_price,
                    underlying_entry_price=excluded.underlying_entry_price,
                    entry_timestamp=excluded.entry_timestamp,
                    status=excluded.status,
                    entry_order_id=excluded.entry_order_id,
                    stop_order_id=excluded.stop_order_id,
                    stop_price=excluded.stop_price,
                    target_order_id=excluded.target_order_id,
                    target_price=excluded.target_price,
                    exit_order_id=excluded.exit_order_id,
                    exit_limit_price=excluded.exit_limit_price,
                    exit_submitted_at=excluded.exit_submitted_at,
                    exit_mode=excluded.exit_mode,
                    updated_at=excluded.updated_at
                """,
                (
                    record.trade_id,
                    record.deployment_id,
                    record.symbol,
                    record.option_symbol,
                    record.quantity,
                    record.entry_price,
                    record.underlying_entry_price,
                    record.entry_timestamp.isoformat() if record.entry_timestamp is not None else None,
                    record.status,
                    record.entry_order_id,
                    record.stop_order_id,
                    record.stop_price,
                    record.target_order_id,
                    record.target_price,
                    record.exit_order_id,
                    record.exit_limit_price,
                    record.exit_submitted_at.isoformat() if record.exit_submitted_at is not None else None,
                    record.exit_mode.value if record.exit_mode is not None else None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def _mark_closed_sync(self, trade_id: str, exit_order_id: str | None) -> None:
        with closing(self.backend.connect()) as conn:
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
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, underlying_entry_price,
                       entry_timestamp, status, entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                       exit_order_id, exit_limit_price, exit_submitted_at, exit_mode
                FROM trade_sessions
                WHERE status != 'closed'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [_trade_record_from_row(row) for row in rows]

    def _get_recent_trades_sync(self, limit: int) -> list[TradeRecord]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, underlying_entry_price,
                       entry_timestamp, status, entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                       exit_order_id, exit_limit_price, exit_submitted_at, exit_mode
                FROM trade_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [_trade_record_from_row(row) for row in rows]


class SQLiteCashBudgetRepository(CashBudgetRepository):
    """SQLite-backed storage for conservative daily cash budgets."""

    def __init__(self, db_path: str, *, backend: SQLiteBackend | None = None) -> None:
        self.db_path = db_path
        self.backend = backend or SQLiteBackend(db_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_day_sync, trade_date)

    async def upsert_day(self, day: CashBudgetDay) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._upsert_day_sync, day)

    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_reservation_sync, trade_id)

    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._upsert_reservation_sync, reservation)

    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._mark_reservation_status_sync, trade_id, status)

    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._reservation_totals_sync, trade_date)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.backend.run_write(self._init_db)
            self._initialized = True

    def _init_db(self) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cash_budget_days (
                    trade_date TEXT PRIMARY KEY,
                    account_type TEXT,
                    broker_cash_only_buying_power REAL NOT NULL,
                    usable_budget REAL NOT NULL,
                    buffer_pct REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cash_budget_reservations (
                    trade_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cash_budget_reservations_trade_date_status
                ON cash_budget_reservations (trade_date, status)
                """
            )
            conn.commit()

    def _get_day_sync(self, trade_date: str) -> CashBudgetDay | None:
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                """
                SELECT trade_date, account_type, broker_cash_only_buying_power, usable_budget, buffer_pct
                FROM cash_budget_days
                WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
        if row is None:
            return None
        return CashBudgetDay(
            trade_date=row[0],
            account_type=row[1],
            broker_cash_only_buying_power=float(row[2]),
            usable_budget=float(row[3]),
            buffer_pct=float(row[4]),
        )

    def _upsert_day_sync(self, day: CashBudgetDay) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO cash_budget_days (
                    trade_date, account_type, broker_cash_only_buying_power, usable_budget, buffer_pct, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    account_type=excluded.account_type,
                    broker_cash_only_buying_power=excluded.broker_cash_only_buying_power,
                    usable_budget=excluded.usable_budget,
                    buffer_pct=excluded.buffer_pct,
                    updated_at=excluded.updated_at
                """,
                (
                    day.trade_date,
                    day.account_type,
                    day.broker_cash_only_buying_power,
                    day.usable_budget,
                    day.buffer_pct,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def _get_reservation_sync(self, trade_id: str) -> CashBudgetReservation | None:
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                """
                SELECT trade_id, trade_date, amount, status
                FROM cash_budget_reservations
                WHERE trade_id = ?
                """,
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return CashBudgetReservation(
            trade_id=row[0],
            trade_date=row[1],
            amount=float(row[2]),
            status=row[3],
        )

    def _upsert_reservation_sync(self, reservation: CashBudgetReservation) -> None:
        now = datetime.now(UTC).isoformat()
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO cash_budget_reservations (
                    trade_id, trade_date, amount, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    amount=excluded.amount,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    reservation.trade_id,
                    reservation.trade_date,
                    reservation.amount,
                    reservation.status,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _mark_reservation_status_sync(self, trade_id: str, status: str) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                UPDATE cash_budget_reservations
                SET status = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (status, datetime.now(UTC).isoformat(), trade_id),
            )
            conn.commit()

    def _reservation_totals_sync(self, trade_date: str) -> dict[str, float]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT status, COALESCE(SUM(amount), 0)
                FROM cash_budget_reservations
                WHERE trade_date = ?
                GROUP BY status
                """,
                (trade_date,),
            ).fetchall()
        totals = {"reserved": 0.0, "consumed": 0.0}
        for status, amount in rows:
            if status in totals:
                totals[status] = float(amount or 0.0)
        return totals


def _trade_record_from_row(row) -> TradeRecord:
    return TradeRecord(
        trade_id=row[0],
        deployment_id=row[1],
        symbol=row[2],
        option_symbol=row[3],
        quantity=row[4],
        entry_price=row[5],
        underlying_entry_price=row[6],
        entry_timestamp=datetime.fromisoformat(row[7]) if row[7] else None,
        status=row[8],
        entry_order_id=row[9],
        stop_order_id=row[10],
        stop_price=row[11],
        target_order_id=row[12],
        target_price=row[13],
        exit_order_id=row[14],
        exit_limit_price=row[15],
        exit_submitted_at=datetime.fromisoformat(row[16]) if row[16] else None,
        exit_mode=ExitMode(row[17]) if row[17] else None,
    )
