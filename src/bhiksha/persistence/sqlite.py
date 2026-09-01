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
from bhiksha.domain.models import (
    CashBudgetDay,
    CashBudgetReservation,
    EntryRiskReservation,
    PartialFillRecord,
    TradeRecord,
)
from bhiksha.options.chain_snapshot import ChainSnapshotAttempt, ContractSnapshotRow
from bhiksha.persistence.repository import (
    CashBudgetRepository,
    ChainSnapshotRepository,
    EventRepository,
    TradeStateRepository,
)

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

    async def run_write(self, operation: Callable[..., ReadResultT], *args) -> ReadResultT:
        await self.ensure_db()
        async with self._write_lock:
            return await asyncio.to_thread(operation, *args)

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
            # Cartographer trigger starts, recovery markers, and terminal
            # outcomes are idempotency records.  A watchdog or a repeated
            # strategy callback may present the same attempt again; the
            # existing append-only ledger is the durable dedupe surface.
            if event_type in {
                "cartographer_signal_attempt",
                "cartographer_signal_attempt_recovery",
                "cartographer_signal_attempt_outcome",
            }:
                attempt_id = str(payload.get("signal_attempt_id") or "")
                if attempt_id:
                    existing = conn.execute(
                        "SELECT payload FROM events WHERE event_type = ?",
                        (event_type,),
                    ).fetchall()
                    for (raw_payload,) in existing:
                        try:
                            prior = json.loads(raw_payload)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if str(prior.get("signal_attempt_id") or "") == attempt_id:
                            return
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

    async def mark_closed(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._mark_closed_sync,
            trade_id,
            exit_order_id,
            exit_price,
            exit_filled_quantity,
            exit_filled_at,
            exit_order_status,
            exit_order_type,
            exit_broker_payload,
            exit_rule,
        )

    async def mark_closed_if_open(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> bool:
        await self._ensure_initialized()
        return await self.backend.run_write(
            self._mark_closed_if_open_sync,
            trade_id,
            exit_order_id,
            exit_price,
            exit_filled_quantity,
            exit_filled_at,
            exit_order_status,
            exit_order_type,
            exit_broker_payload,
            exit_rule,
        )

    async def get_open_trades(self) -> list[TradeRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_open_trades_sync)

    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_recent_trades_sync, limit)

    async def record_partial_fill(self, record: PartialFillRecord) -> int:
        await self._ensure_initialized()
        return await self.backend.run_write(self._record_partial_fill_sync, record)

    async def enrich_partial_fill(
        self,
        record_id: int,
        *,
        fill_price: float | None = None,
        fill_quantity: int | None = None,
        filled_at: datetime | None = None,
        order_status: str | None = None,
        order_type: str | None = None,
        broker_payload: dict[str, Any] | None = None,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._enrich_partial_fill_sync,
            record_id,
            fill_price,
            fill_quantity,
            filled_at,
            order_status,
            order_type,
            broker_payload,
        )

    async def get_unconfirmed_partial_fills(self, *, limit: int = 200) -> list[PartialFillRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_unconfirmed_partial_fills_sync, limit)

    async def get_partial_fills(self, trade_id: str) -> list[PartialFillRecord]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_partial_fills_sync, trade_id)

    async def get_partial_fills_for_trades(
        self, trade_ids: list[str]
    ) -> dict[str, list[PartialFillRecord]]:
        await self._ensure_initialized()
        unique_ids = list(dict.fromkeys(str(trade_id) for trade_id in trade_ids if trade_id))
        if not unique_ids:
            return {}
        return await self.backend.run_read(self._get_partial_fills_for_trades_sync, unique_ids)

    async def increment_partial_fill_enrich_attempts(self, record_id: int) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._increment_partial_fill_enrich_attempts_sync, record_id)

    async def mark_partial_fill_abandoned(self, record_id: int, *, reason: str) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._mark_partial_fill_abandoned_sync, record_id, reason)

    async def upsert_entry_risk_reservation(self, reservation: EntryRiskReservation) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._upsert_entry_risk_reservation_sync, reservation)

    async def get_active_entry_risk_reservations(self) -> list[EntryRiskReservation]:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_active_entry_risk_reservations_sync)

    async def mark_entry_risk_reservation_status(self, trade_id: str, status: str) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._mark_entry_risk_reservation_status_sync,
            trade_id,
            status,
        )

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
                    exit_price REAL,
                    exit_filled_quantity INTEGER,
                    exit_filled_at TEXT,
                    exit_order_status TEXT,
                    exit_order_type TEXT,
                    exit_broker_payload TEXT,
                    exit_rule TEXT,
                    can_ladder INTEGER,
                    active_plan_id TEXT,
                    research_run_id TEXT,
                    evidence_packet_id TEXT,
                    evidence_artifact_sha256 TEXT,
                    evidence_artifact_uri TEXT,
                    experiment_id TEXT,
                    cohort_id TEXT,
                    cohort_contract_sha256 TEXT,
                    deployment_contract_sha256 TEXT,
                    declared_option_selection_contract_id TEXT,
                    declared_option_selection_contract_sha256 TEXT,
                    authorization_identity_status TEXT,
                    exit_policy_id TEXT,
                    exit_policy_sha256 TEXT,
                    option_selection_snapshot_id TEXT,
                    option_selection_snapshot_persisted INTEGER,
                    option_candidate_set_sha256 TEXT,
                    actual_option_selection_sha256 TEXT,
                    canary_id TEXT,
                    canary_authorization_sha256 TEXT,
                    canary_start_at TEXT,
                    canary_expires_at TEXT,
                    plan_revision_id TEXT,
                    session_id TEXT,
                    fact_receipt_id TEXT,
                    frozen_entry_risk_usd REAL,
                    frozen_round_trip_cost_usd REAL,
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
            if "exit_price" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_price REAL")
            if "exit_filled_quantity" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_filled_quantity INTEGER")
            if "exit_filled_at" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_filled_at TEXT")
            if "exit_order_status" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_order_status TEXT")
            if "exit_order_type" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_order_type TEXT")
            if "exit_broker_payload" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_broker_payload TEXT")
            if "exit_rule" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN exit_rule TEXT")
            if "can_ladder" not in existing_columns:
                conn.execute("ALTER TABLE trade_sessions ADD COLUMN can_ladder INTEGER")
            identity_columns = {
                "active_plan_id": "TEXT",
                "research_run_id": "TEXT",
                "evidence_packet_id": "TEXT",
                "evidence_artifact_sha256": "TEXT",
                "evidence_artifact_uri": "TEXT",
                "experiment_id": "TEXT",
                "cohort_id": "TEXT",
                "cohort_contract_sha256": "TEXT",
                "deployment_contract_sha256": "TEXT",
                "declared_option_selection_contract_id": "TEXT",
                "declared_option_selection_contract_sha256": "TEXT",
                "authorization_identity_status": "TEXT",
                "exit_policy_id": "TEXT",
                "exit_policy_sha256": "TEXT",
                "option_selection_snapshot_id": "TEXT",
                "option_selection_snapshot_persisted": "INTEGER",
                "option_candidate_set_sha256": "TEXT",
                "actual_option_selection_sha256": "TEXT",
                "canary_id": "TEXT",
                "canary_authorization_sha256": "TEXT",
                "canary_start_at": "TEXT",
                "canary_expires_at": "TEXT",
                "plan_revision_id": "TEXT",
                "session_id": "TEXT",
                "fact_receipt_id": "TEXT",
                "frozen_entry_risk_usd": "REAL",
                "frozen_round_trip_cost_usd": "REAL",
            }
            for column_name, column_type in identity_columns.items():
                if column_name not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE trade_sessions ADD COLUMN {column_name} {column_type}"
                    )
            # get_recent_trades orders by updated_at DESC on every risk-manager
            # consult (2-3x per entry attempt + once per bar); without this
            # index that is a full-table scan + temp b-tree sort that grows
            # with trade history (2026-07-02 audit finding).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_sessions_updated_at ON trade_sessions(updated_at)"
            )
            # ITEM B (2026-07-08 hygiene batch): one durable row per banked
            # partial leg. trade_sessions.quantity is overwritten to the
            # residual on every partial bank (see
            # ``_handle_partial_scale_locked``), so this is the only durable
            # home for a banked leg's closed_quantity and its confirmed fill
            # price/quantity/timestamp -- previously only an unfilled-truth
            # order_id survived, in the append-only event log.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_partial_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    option_symbol TEXT,
                    closed_quantity INTEGER NOT NULL,
                    order_id TEXT,
                    exit_rule TEXT,
                    submitted_at TEXT,
                    fill_price REAL,
                    fill_quantity INTEGER,
                    filled_at TEXT,
                    order_status TEXT,
                    order_type TEXT,
                    broker_payload TEXT,
                    origin TEXT NOT NULL DEFAULT 'partial_scale',
                    enrich_attempts INTEGER NOT NULL DEFAULT 0,
                    abandoned_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Audit fixes A.2/3 (2026-07-08): additive columns for a db created
            # by the pre-audit revision of this same branch (IF-NOT-EXISTS
            # pattern, matching trade_sessions above).
            partial_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(trade_partial_fills)").fetchall()
            }
            if "origin" not in partial_columns:
                conn.execute(
                    "ALTER TABLE trade_partial_fills ADD COLUMN origin TEXT NOT NULL DEFAULT 'partial_scale'"
                )
            if "enrich_attempts" not in partial_columns:
                conn.execute(
                    "ALTER TABLE trade_partial_fills ADD COLUMN enrich_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "abandoned_reason" not in partial_columns:
                conn.execute("ALTER TABLE trade_partial_fills ADD COLUMN abandoned_reason TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_partial_fills_trade_id ON trade_partial_fills(trade_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entry_risk_reservations (
                    trade_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    cluster TEXT,
                    planned_stop_loss_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entry_risk_reservations_status
                ON entry_risk_reservations(status)
                """
            )
            conn.commit()

    def _upsert_entry_risk_reservation_sync(
        self,
        reservation: EntryRiskReservation,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO entry_risk_reservations (
                    trade_id, deployment_id, symbol, cluster,
                    planned_stop_loss_usd, status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    deployment_id=excluded.deployment_id,
                    symbol=excluded.symbol,
                    cluster=excluded.cluster,
                    planned_stop_loss_usd=excluded.planned_stop_loss_usd,
                    status=excluded.status,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    reservation.trade_id,
                    reservation.deployment_id,
                    reservation.symbol,
                    reservation.cluster,
                    reservation.planned_stop_loss_usd,
                    reservation.status,
                    reservation.expires_at.isoformat(),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_active_entry_risk_reservations_sync(self) -> list[EntryRiskReservation]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT trade_id, deployment_id, symbol, cluster,
                       planned_stop_loss_usd, status, expires_at
                FROM entry_risk_reservations
                WHERE status = 'reserved'
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_sessions
                      WHERE trade_sessions.trade_id = entry_risk_reservations.trade_id
                        AND lower(trade_sessions.status) = 'closed'
                  )
                ORDER BY created_at, trade_id
                """
            ).fetchall()
        return [
            EntryRiskReservation(
                trade_id=row[0],
                deployment_id=row[1],
                symbol=row[2],
                cluster=row[3],
                planned_stop_loss_usd=float(row[4]),
                status=row[5],
                expires_at=datetime.fromisoformat(row[6]),
            )
            for row in rows
        ]

    def _mark_entry_risk_reservation_status_sync(self, trade_id: str, status: str) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                UPDATE entry_risk_reservations
                SET status = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (status, datetime.now(UTC).isoformat(), trade_id),
            )
            conn.commit()

    def _upsert_trade_sync(self, record: TradeRecord) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO trade_sessions (
                    trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, underlying_entry_price,
                    entry_timestamp, status, entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                    exit_order_id, exit_limit_price, exit_submitted_at, exit_mode, exit_price, exit_filled_quantity,
                    exit_filled_at, exit_order_status, exit_order_type, exit_broker_payload, exit_rule, can_ladder,
                    active_plan_id, research_run_id, evidence_packet_id, evidence_artifact_sha256,
                    evidence_artifact_uri, experiment_id, cohort_id, cohort_contract_sha256,
                    deployment_contract_sha256, declared_option_selection_contract_id,
                    declared_option_selection_contract_sha256, authorization_identity_status,
                    exit_policy_id, exit_policy_sha256, option_selection_snapshot_id,
                    option_selection_snapshot_persisted, option_candidate_set_sha256,
                    actual_option_selection_sha256, canary_id, canary_authorization_sha256,
                    canary_start_at, canary_expires_at,
                    plan_revision_id, session_id, fact_receipt_id,
                    frozen_entry_risk_usd, frozen_round_trip_cost_usd,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    deployment_id=excluded.deployment_id,
                    symbol=excluded.symbol,
                    option_symbol=excluded.option_symbol,
                    quantity=excluded.quantity,
                    entry_price=excluded.entry_price,
                    underlying_entry_price=excluded.underlying_entry_price,
                    entry_timestamp=excluded.entry_timestamp,
                    status=CASE
                        WHEN trade_sessions.status='exit_pending'
                             AND excluded.status IN ('open_protected', 'open_unprotected')
                             AND excluded.exit_order_id IS NULL
                             AND excluded.exit_submitted_at IS NULL
                             AND excluded.exit_mode IS NULL
                        THEN trade_sessions.status
                        ELSE excluded.status
                    END,
                    entry_order_id=excluded.entry_order_id,
                    stop_order_id=excluded.stop_order_id,
                    stop_price=excluded.stop_price,
                    target_order_id=excluded.target_order_id,
                    target_price=excluded.target_price,
                    exit_order_id=CASE
                        WHEN trade_sessions.status='exit_pending'
                             AND excluded.status IN ('open_protected', 'open_unprotected')
                             AND excluded.exit_order_id IS NULL
                             AND excluded.exit_submitted_at IS NULL
                             AND excluded.exit_mode IS NULL
                        THEN trade_sessions.exit_order_id
                        ELSE excluded.exit_order_id
                    END,
                    exit_limit_price=CASE
                        WHEN trade_sessions.status='exit_pending'
                             AND excluded.status IN ('open_protected', 'open_unprotected')
                             AND excluded.exit_order_id IS NULL
                             AND excluded.exit_submitted_at IS NULL
                             AND excluded.exit_mode IS NULL
                        THEN trade_sessions.exit_limit_price
                        ELSE excluded.exit_limit_price
                    END,
                    exit_submitted_at=CASE
                        WHEN trade_sessions.status='exit_pending'
                             AND excluded.status IN ('open_protected', 'open_unprotected')
                             AND excluded.exit_order_id IS NULL
                             AND excluded.exit_submitted_at IS NULL
                             AND excluded.exit_mode IS NULL
                        THEN trade_sessions.exit_submitted_at
                        ELSE excluded.exit_submitted_at
                    END,
                    exit_mode=CASE
                        WHEN trade_sessions.status='exit_pending'
                             AND excluded.status IN ('open_protected', 'open_unprotected')
                             AND excluded.exit_order_id IS NULL
                             AND excluded.exit_submitted_at IS NULL
                             AND excluded.exit_mode IS NULL
                        THEN trade_sessions.exit_mode
                        ELSE excluded.exit_mode
                    END,
                    exit_price=COALESCE(excluded.exit_price, trade_sessions.exit_price),
                    exit_filled_quantity=COALESCE(excluded.exit_filled_quantity, trade_sessions.exit_filled_quantity),
                    exit_filled_at=COALESCE(excluded.exit_filled_at, trade_sessions.exit_filled_at),
                    exit_order_status=COALESCE(excluded.exit_order_status, trade_sessions.exit_order_status),
                    exit_order_type=COALESCE(excluded.exit_order_type, trade_sessions.exit_order_type),
                    exit_broker_payload=COALESCE(excluded.exit_broker_payload, trade_sessions.exit_broker_payload),
                    exit_rule=COALESCE(excluded.exit_rule, trade_sessions.exit_rule),
                    can_ladder=COALESCE(excluded.can_ladder, trade_sessions.can_ladder),
                    active_plan_id=COALESCE(trade_sessions.active_plan_id, excluded.active_plan_id),
                    research_run_id=COALESCE(trade_sessions.research_run_id, excluded.research_run_id),
                    evidence_packet_id=COALESCE(trade_sessions.evidence_packet_id, excluded.evidence_packet_id),
                    evidence_artifact_sha256=COALESCE(trade_sessions.evidence_artifact_sha256, excluded.evidence_artifact_sha256),
                    evidence_artifact_uri=COALESCE(trade_sessions.evidence_artifact_uri, excluded.evidence_artifact_uri),
                    experiment_id=COALESCE(trade_sessions.experiment_id, excluded.experiment_id),
                    cohort_id=COALESCE(trade_sessions.cohort_id, excluded.cohort_id),
                    cohort_contract_sha256=COALESCE(trade_sessions.cohort_contract_sha256, excluded.cohort_contract_sha256),
                    deployment_contract_sha256=COALESCE(trade_sessions.deployment_contract_sha256, excluded.deployment_contract_sha256),
                    declared_option_selection_contract_id=COALESCE(trade_sessions.declared_option_selection_contract_id, excluded.declared_option_selection_contract_id),
                    declared_option_selection_contract_sha256=COALESCE(trade_sessions.declared_option_selection_contract_sha256, excluded.declared_option_selection_contract_sha256),
                    authorization_identity_status=COALESCE(trade_sessions.authorization_identity_status, excluded.authorization_identity_status),
                    exit_policy_id=COALESCE(trade_sessions.exit_policy_id, excluded.exit_policy_id),
                    exit_policy_sha256=COALESCE(trade_sessions.exit_policy_sha256, excluded.exit_policy_sha256),
                    option_selection_snapshot_id=COALESCE(trade_sessions.option_selection_snapshot_id, excluded.option_selection_snapshot_id),
                    option_selection_snapshot_persisted=COALESCE(trade_sessions.option_selection_snapshot_persisted, excluded.option_selection_snapshot_persisted),
                    option_candidate_set_sha256=COALESCE(trade_sessions.option_candidate_set_sha256, excluded.option_candidate_set_sha256),
                    actual_option_selection_sha256=COALESCE(trade_sessions.actual_option_selection_sha256, excluded.actual_option_selection_sha256),
                    canary_id=COALESCE(trade_sessions.canary_id, excluded.canary_id),
                    canary_authorization_sha256=COALESCE(trade_sessions.canary_authorization_sha256, excluded.canary_authorization_sha256),
                    canary_start_at=COALESCE(trade_sessions.canary_start_at, excluded.canary_start_at),
                    canary_expires_at=COALESCE(trade_sessions.canary_expires_at, excluded.canary_expires_at),
                    plan_revision_id=COALESCE(trade_sessions.plan_revision_id, excluded.plan_revision_id),
                    session_id=COALESCE(trade_sessions.session_id, excluded.session_id),
                    fact_receipt_id=COALESCE(trade_sessions.fact_receipt_id, excluded.fact_receipt_id),
                    frozen_entry_risk_usd=COALESCE(trade_sessions.frozen_entry_risk_usd, excluded.frozen_entry_risk_usd),
                    frozen_round_trip_cost_usd=COALESCE(trade_sessions.frozen_round_trip_cost_usd, excluded.frozen_round_trip_cost_usd),
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
                    record.exit_price,
                    record.exit_filled_quantity,
                    record.exit_filled_at.isoformat() if record.exit_filled_at is not None else None,
                    record.exit_order_status,
                    record.exit_order_type,
                    json.dumps(record.exit_broker_payload, default=str) if record.exit_broker_payload is not None else None,
                    record.exit_rule,
                    None if record.can_ladder is None else int(record.can_ladder),
                    record.active_plan_id,
                    record.research_run_id,
                    record.evidence_packet_id,
                    record.evidence_artifact_sha256,
                    record.evidence_artifact_uri,
                    record.experiment_id,
                    record.cohort_id,
                    record.cohort_contract_sha256,
                    record.deployment_contract_sha256,
                    record.declared_option_selection_contract_id,
                    record.declared_option_selection_contract_sha256,
                    record.authorization_identity_status,
                    record.exit_policy_id,
                    record.exit_policy_sha256,
                    record.option_selection_snapshot_id,
                    None if record.option_selection_snapshot_persisted is None else int(record.option_selection_snapshot_persisted),
                    record.option_candidate_set_sha256,
                    record.actual_option_selection_sha256,
                    record.canary_id,
                    record.canary_authorization_sha256,
                    record.canary_start_at,
                    record.canary_expires_at,
                    record.plan_revision_id,
                    record.session_id,
                    record.fact_receipt_id,
                    record.frozen_entry_risk_usd,
                    record.frozen_round_trip_cost_usd,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def _mark_closed_sync(
        self,
        trade_id: str,
        exit_order_id: str | None,
        exit_price: float | None,
        exit_filled_quantity: int | None,
        exit_filled_at: datetime | None,
        exit_order_status: str | None,
        exit_order_type: str | None,
        exit_broker_payload: dict[str, Any] | None,
        exit_rule: str | None = None,
    ) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                UPDATE trade_sessions
                SET status = ?,
                    exit_order_id = COALESCE(?, exit_order_id),
                    exit_price = COALESCE(?, exit_price),
                    exit_filled_quantity = COALESCE(?, exit_filled_quantity),
                    exit_filled_at = COALESCE(?, exit_filled_at),
                    exit_order_status = COALESCE(?, exit_order_status),
                    exit_order_type = COALESCE(?, exit_order_type),
                    exit_broker_payload = COALESCE(?, exit_broker_payload),
                    exit_rule = COALESCE(?, exit_rule),
                    updated_at = ?
                WHERE trade_id = ?
                """,
                (
                    "closed",
                    exit_order_id,
                    exit_price,
                    exit_filled_quantity,
                    exit_filled_at.isoformat() if exit_filled_at is not None else None,
                    exit_order_status,
                    exit_order_type,
                    json.dumps(exit_broker_payload, default=str) if exit_broker_payload is not None else None,
                    exit_rule,
                    datetime.now(UTC).isoformat(),
                    trade_id,
                ),
            )
            conn.commit()

    def _mark_closed_if_open_sync(
        self,
        trade_id: str,
        exit_order_id: str | None,
        exit_price: float | None,
        exit_filled_quantity: int | None,
        exit_filled_at: datetime | None,
        exit_order_status: str | None,
        exit_order_type: str | None,
        exit_broker_payload: dict[str, Any] | None,
        exit_rule: str | None = None,
    ) -> bool:
        with closing(self.backend.connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE trade_sessions
                SET status = ?,
                    exit_order_id = COALESCE(?, exit_order_id),
                    exit_price = COALESCE(?, exit_price),
                    exit_filled_quantity = COALESCE(?, exit_filled_quantity),
                    exit_filled_at = COALESCE(?, exit_filled_at),
                    exit_order_status = COALESCE(?, exit_order_status),
                    exit_order_type = COALESCE(?, exit_order_type),
                    exit_broker_payload = COALESCE(?, exit_broker_payload),
                    exit_rule = COALESCE(?, exit_rule),
                    updated_at = ?
                WHERE trade_id = ? AND lower(status) != 'closed'
                """,
                (
                    "closed",
                    exit_order_id,
                    exit_price,
                    exit_filled_quantity,
                    exit_filled_at.isoformat() if exit_filled_at is not None else None,
                    exit_order_status,
                    exit_order_type,
                    json.dumps(exit_broker_payload, default=str)
                    if exit_broker_payload is not None
                    else None,
                    exit_rule,
                    datetime.now(UTC).isoformat(),
                    trade_id,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def _get_open_trades_sync(self) -> list[TradeRecord]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT trade_id, deployment_id, symbol, option_symbol, quantity, entry_price, underlying_entry_price,
                       entry_timestamp, status, entry_order_id, stop_order_id, stop_price, target_order_id, target_price,
                       exit_order_id, exit_limit_price, exit_submitted_at, exit_mode, exit_price, exit_filled_quantity,
                       exit_filled_at, exit_order_status, exit_order_type, exit_broker_payload, exit_rule, can_ladder,
                       active_plan_id, research_run_id, evidence_packet_id, evidence_artifact_sha256,
                       evidence_artifact_uri, experiment_id, cohort_id, cohort_contract_sha256,
                       deployment_contract_sha256, declared_option_selection_contract_id,
                       declared_option_selection_contract_sha256, authorization_identity_status,
                       exit_policy_id, exit_policy_sha256, option_selection_snapshot_id,
                       option_selection_snapshot_persisted, option_candidate_set_sha256,
                       actual_option_selection_sha256, canary_id, canary_authorization_sha256,
                       canary_start_at, canary_expires_at, plan_revision_id,
                       session_id, fact_receipt_id, frozen_entry_risk_usd,
                       frozen_round_trip_cost_usd
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
                       exit_order_id, exit_limit_price, exit_submitted_at, exit_mode, exit_price, exit_filled_quantity,
                       exit_filled_at, exit_order_status, exit_order_type, exit_broker_payload, exit_rule, can_ladder,
                       active_plan_id, research_run_id, evidence_packet_id, evidence_artifact_sha256,
                       evidence_artifact_uri, experiment_id, cohort_id, cohort_contract_sha256,
                       deployment_contract_sha256, declared_option_selection_contract_id,
                       declared_option_selection_contract_sha256, authorization_identity_status,
                       exit_policy_id, exit_policy_sha256, option_selection_snapshot_id,
                       option_selection_snapshot_persisted, option_candidate_set_sha256,
                       actual_option_selection_sha256, canary_id, canary_authorization_sha256,
                       canary_start_at, canary_expires_at, plan_revision_id,
                       session_id, fact_receipt_id, frozen_entry_risk_usd,
                       frozen_round_trip_cost_usd
                FROM trade_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [_trade_record_from_row(row) for row in rows]

    def _record_partial_fill_sync(self, record: PartialFillRecord) -> int:
        with closing(self.backend.connect()) as conn:
            if record.order_id is not None:
                existing = conn.execute(
                    """
                    SELECT id FROM trade_partial_fills
                    WHERE trade_id = ? AND order_id = ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (record.trade_id, record.order_id),
                ).fetchone()
                if existing is not None:
                    return int(existing[0])
            now = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO trade_partial_fills (
                    trade_id, deployment_id, symbol, option_symbol, closed_quantity, order_id, exit_rule,
                    submitted_at, fill_price, fill_quantity, filled_at, order_status, order_type, broker_payload,
                    origin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trade_id,
                    record.deployment_id,
                    record.symbol,
                    record.option_symbol,
                    record.closed_quantity,
                    record.order_id,
                    record.exit_rule,
                    record.submitted_at.isoformat() if record.submitted_at is not None else None,
                    record.fill_price,
                    record.fill_quantity,
                    record.filled_at.isoformat() if record.filled_at is not None else None,
                    record.order_status,
                    record.order_type,
                    json.dumps(record.broker_payload, default=str) if record.broker_payload is not None else None,
                    record.origin,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _enrich_partial_fill_sync(
        self,
        record_id: int,
        fill_price: float | None,
        fill_quantity: int | None,
        filled_at: datetime | None,
        order_status: str | None,
        order_type: str | None,
        broker_payload: dict[str, Any] | None,
    ) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                UPDATE trade_partial_fills
                SET fill_price = COALESCE(?, fill_price),
                    fill_quantity = COALESCE(?, fill_quantity),
                    filled_at = COALESCE(?, filled_at),
                    order_status = COALESCE(?, order_status),
                    order_type = COALESCE(?, order_type),
                    broker_payload = COALESCE(?, broker_payload),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    fill_price,
                    fill_quantity,
                    filled_at.isoformat() if filled_at is not None else None,
                    order_status,
                    order_type,
                    json.dumps(broker_payload, default=str) if broker_payload is not None else None,
                    datetime.now(UTC).isoformat(),
                    record_id,
                ),
            )
            conn.commit()

    def _get_unconfirmed_partial_fills_sync(self, limit: int) -> list[PartialFillRecord]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, trade_id, deployment_id, symbol, option_symbol, closed_quantity, order_id, exit_rule,
                       submitted_at, fill_price, fill_quantity, filled_at, order_status, order_type, broker_payload,
                       origin, enrich_attempts, abandoned_reason
                FROM trade_partial_fills
                WHERE fill_price IS NULL AND abandoned_reason IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [_partial_fill_record_from_row(row) for row in rows]

    def _get_partial_fills_sync(self, trade_id: str) -> list[PartialFillRecord]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, trade_id, deployment_id, symbol, option_symbol, closed_quantity, order_id, exit_rule,
                       submitted_at, fill_price, fill_quantity, filled_at, order_status, order_type, broker_payload,
                       origin, enrich_attempts, abandoned_reason
                FROM trade_partial_fills
                WHERE trade_id = ?
                ORDER BY id ASC
                """,
                (trade_id,),
            ).fetchall()
        return [_partial_fill_record_from_row(row) for row in rows]

    def _get_partial_fills_for_trades_sync(
        self, trade_ids: list[str]
    ) -> dict[str, list[PartialFillRecord]]:
        grouped: dict[str, list[PartialFillRecord]] = {trade_id: [] for trade_id in trade_ids}
        with closing(self.backend.connect()) as conn:
            # Stay below conservative SQLite host-parameter limits while using
            # one connection/read operation for the live risk consultation.
            for offset in range(0, len(trade_ids), 500):
                chunk = trade_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT id, trade_id, deployment_id, symbol, option_symbol, closed_quantity, order_id, exit_rule,
                           submitted_at, fill_price, fill_quantity, filled_at, order_status, order_type, broker_payload,
                           origin, enrich_attempts, abandoned_reason
                    FROM trade_partial_fills
                    WHERE trade_id IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    record = _partial_fill_record_from_row(row)
                    grouped[record.trade_id].append(record)
        return grouped

    def _increment_partial_fill_enrich_attempts_sync(self, record_id: int) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                "UPDATE trade_partial_fills SET enrich_attempts = enrich_attempts + 1, updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), record_id),
            )
            conn.commit()

    def _mark_partial_fill_abandoned_sync(self, record_id: int, reason: str) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                "UPDATE trade_partial_fills SET abandoned_reason = ?, updated_at = ? WHERE id = ?",
                (reason, datetime.now(UTC).isoformat(), record_id),
            )
            conn.commit()


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


class SQLiteChainSnapshotRepository(ChainSnapshotRepository):
    """SQLite-backed storage for per-attempt option-chain snapshots.

    Two tables, written together per attempt:
      * ``option_chain_snapshot_attempts`` -- one row per attempt (the
        request context: thresholds, totals, outcome).
      * ``option_chain_snapshots`` -- one row per captured candidate contract
        (bounded to the correct type + DTE window/nearest-after expiry, see
        options/chain_snapshot.py), carrying that contract's verdict plus a
        denormalized copy of the attempt's outcome for query convenience.

    ``attempt.snapshot_id`` (the attempts table's primary key) is a fresh
    ``uuid4`` generated once per ``plan_entry`` call (see
    execution/planner.py) -- both inserts below are plain, unconditional
    INSERTs on that assumption. A collision would raise before either table's
    write commits (the transaction covers both), which the caller's
    never-fail wrapper then drops as a whole rather than leaving a
    summary/per-contract mismatch behind.
    """

    def __init__(self, db_path: str, *, backend: SQLiteBackend | None = None) -> None:
        self.db_path = db_path
        self.backend = backend or SQLiteBackend(db_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def record_attempt(self, attempt: ChainSnapshotAttempt) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._record_attempt_sync, attempt)

    async def purge_older_than(self, cutoff: datetime) -> int:
        await self._ensure_initialized()
        return await self.backend.run_write(self._purge_older_than_sync, cutoff.isoformat())

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
                CREATE TABLE IF NOT EXISTS option_chain_snapshot_attempts (
                    snapshot_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    allowed_contract_type TEXT NOT NULL,
                    dte_min INTEGER NOT NULL,
                    dte_max INTEGER NOT NULL,
                    min_open_interest INTEGER NOT NULL,
                    target_abs_delta_min REAL,
                    target_abs_delta_max REAL,
                    max_bid_ask_spread_pct REAL,
                    dte_fallback_policy TEXT NOT NULL,
                    nearest_after_dte INTEGER,
                    total_candidates INTEGER NOT NULL,
                    captured_candidates INTEGER NOT NULL,
                    selector_empty INTEGER NOT NULL,
                    selected_option_symbol TEXT,
                    option_candidate_set_sha256 TEXT,
                    actual_option_selection_sha256 TEXT
                )
                """
            )
            attempt_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(option_chain_snapshot_attempts)"
                ).fetchall()
            }
            for column_name in (
                "option_candidate_set_sha256",
                "actual_option_selection_sha256",
            ):
                if column_name not in attempt_columns:
                    conn.execute(
                        f"ALTER TABLE option_chain_snapshot_attempts ADD COLUMN {column_name} TEXT"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshot_attempts_created_at "
                "ON option_chain_snapshot_attempts(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshot_attempts_symbol "
                "ON option_chain_snapshot_attempts(symbol, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshot_attempts_deployment "
                "ON option_chain_snapshot_attempts(deployment_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_chain_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    option_symbol TEXT NOT NULL,
                    expiration_date TEXT,
                    dte INTEGER,
                    strike REAL,
                    contract_type TEXT,
                    open_interest INTEGER,
                    delta REAL,
                    bid REAL,
                    ask REAL,
                    spread_pct REAL,
                    dte_in_window INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    fallback_verdict TEXT,
                    is_selected INTEGER NOT NULL,
                    attempt_selected_option_symbol TEXT,
                    attempt_selector_empty INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_snapshot_id "
                "ON option_chain_snapshots(snapshot_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_created_at "
                "ON option_chain_snapshots(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_symbol "
                "ON option_chain_snapshots(symbol, created_at)"
            )
            conn.commit()

    def _record_attempt_sync(self, attempt: ChainSnapshotAttempt) -> None:
        created_at = datetime.now(UTC).isoformat()
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO option_chain_snapshot_attempts (
                    snapshot_id, created_at, deployment_id, symbol, lane, direction, allowed_contract_type,
                    dte_min, dte_max, min_open_interest, target_abs_delta_min, target_abs_delta_max,
                    max_bid_ask_spread_pct, dte_fallback_policy, nearest_after_dte, total_candidates,
                    captured_candidates, selector_empty, selected_option_symbol
                    , option_candidate_set_sha256, actual_option_selection_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.snapshot_id,
                    created_at,
                    attempt.deployment_id,
                    attempt.symbol,
                    attempt.lane,
                    attempt.direction,
                    attempt.allowed_contract_type,
                    attempt.dte_min,
                    attempt.dte_max,
                    attempt.min_open_interest,
                    attempt.target_abs_delta_min,
                    attempt.target_abs_delta_max,
                    attempt.max_bid_ask_spread_pct,
                    attempt.dte_fallback_policy,
                    attempt.nearest_after_dte,
                    attempt.total_candidates,
                    attempt.captured_candidates,
                    int(attempt.selector_empty),
                    attempt.selected_option_symbol,
                    attempt.option_candidate_set_sha256,
                    attempt.actual_option_selection_sha256,
                ),
            )
            if attempt.rows:
                conn.executemany(
                    """
                    INSERT INTO option_chain_snapshots (
                        snapshot_id, created_at, deployment_id, symbol, lane, option_symbol, expiration_date,
                        dte, strike, contract_type, open_interest, delta, bid, ask, spread_pct, dte_in_window,
                        verdict, fallback_verdict, is_selected, attempt_selected_option_symbol,
                        attempt_selector_empty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        _contract_row_params(attempt, row, created_at)
                        for row in attempt.rows
                    ],
                )
            conn.commit()

    def _purge_older_than_sync(self, cutoff_iso: str) -> int:
        with closing(self.backend.connect()) as conn:
            contract_cursor = conn.execute(
                "DELETE FROM option_chain_snapshots WHERE created_at < ?",
                (cutoff_iso,),
            )
            attempt_cursor = conn.execute(
                "DELETE FROM option_chain_snapshot_attempts WHERE created_at < ?",
                (cutoff_iso,),
            )
            conn.commit()
            return int(contract_cursor.rowcount or 0) + int(attempt_cursor.rowcount or 0)


def _contract_row_params(attempt: ChainSnapshotAttempt, row: ContractSnapshotRow, created_at: str) -> tuple:
    return (
        attempt.snapshot_id,
        created_at,
        attempt.deployment_id,
        attempt.symbol,
        attempt.lane,
        row.option_symbol,
        row.expiration_date,
        row.dte,
        row.strike,
        row.contract_type,
        row.open_interest,
        row.delta,
        row.bid,
        row.ask,
        row.spread_pct,
        int(row.dte_in_window),
        row.verdict,
        row.fallback_verdict,
        int(row.is_selected),
        attempt.selected_option_symbol,
        int(attempt.selector_empty),
    )


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
        exit_price=row[18] if len(row) > 18 else None,
        exit_filled_quantity=row[19] if len(row) > 19 else None,
        exit_filled_at=datetime.fromisoformat(row[20]) if len(row) > 20 and row[20] else None,
        exit_order_status=row[21] if len(row) > 21 else None,
        exit_order_type=row[22] if len(row) > 22 else None,
        exit_broker_payload=json.loads(row[23]) if len(row) > 23 and row[23] else None,
        exit_rule=row[24] if len(row) > 24 else None,
        can_ladder=(bool(row[25]) if row[25] is not None else None) if len(row) > 25 else None,
        active_plan_id=row[26] if len(row) > 26 else None,
        research_run_id=row[27] if len(row) > 27 else None,
        evidence_packet_id=row[28] if len(row) > 28 else None,
        evidence_artifact_sha256=row[29] if len(row) > 29 else None,
        evidence_artifact_uri=row[30] if len(row) > 30 else None,
        experiment_id=row[31] if len(row) > 31 else None,
        cohort_id=row[32] if len(row) > 32 else None,
        cohort_contract_sha256=row[33] if len(row) > 33 else None,
        deployment_contract_sha256=row[34] if len(row) > 34 else None,
        declared_option_selection_contract_id=row[35] if len(row) > 35 else None,
        declared_option_selection_contract_sha256=row[36] if len(row) > 36 else None,
        authorization_identity_status=row[37] if len(row) > 37 else None,
        exit_policy_id=row[38] if len(row) > 38 else None,
        exit_policy_sha256=row[39] if len(row) > 39 else None,
        option_selection_snapshot_id=row[40] if len(row) > 40 else None,
        option_selection_snapshot_persisted=(
            bool(row[41]) if len(row) > 41 and row[41] is not None else None
        ),
        option_candidate_set_sha256=row[42] if len(row) > 42 else None,
        actual_option_selection_sha256=row[43] if len(row) > 43 else None,
        canary_id=row[44] if len(row) > 44 else None,
        canary_authorization_sha256=row[45] if len(row) > 45 else None,
        canary_start_at=row[46] if len(row) > 46 else None,
        canary_expires_at=row[47] if len(row) > 47 else None,
        plan_revision_id=row[48] if len(row) > 48 else None,
        session_id=row[49] if len(row) > 49 else None,
        fact_receipt_id=row[50] if len(row) > 50 else None,
        frozen_entry_risk_usd=row[51] if len(row) > 51 else None,
        frozen_round_trip_cost_usd=row[52] if len(row) > 52 else None,
    )


def _partial_fill_record_from_row(row) -> PartialFillRecord:
    return PartialFillRecord(
        id=row[0],
        trade_id=row[1],
        deployment_id=row[2],
        symbol=row[3],
        option_symbol=row[4],
        closed_quantity=row[5],
        order_id=row[6],
        exit_rule=row[7],
        submitted_at=datetime.fromisoformat(row[8]) if row[8] else None,
        fill_price=row[9],
        fill_quantity=row[10],
        filled_at=datetime.fromisoformat(row[11]) if row[11] else None,
        order_status=row[12],
        order_type=row[13],
        broker_payload=json.loads(row[14]) if row[14] else None,
        origin=(row[15] or "partial_scale") if len(row) > 15 else "partial_scale",
        enrich_attempts=(row[16] or 0) if len(row) > 16 else 0,
        abandoned_reason=row[17] if len(row) > 17 else None,
    )
