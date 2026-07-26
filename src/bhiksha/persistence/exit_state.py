"""SQLite persistence for frozen exit policy and restart-safe action state."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

from bhiksha.domain.exit_state import (
    ExitActionIntent,
    ExitRuntimeState,
    TradeExitPolicySnapshot,
)
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.persistence.sqlite import SQLiteBackend


def inspect_risk_envelope_rollback_latches(
    database_path: str | Path,
    *,
    deployment_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Read durable canary rollback state without creating or mutating SQLite."""

    path = Path(database_path)
    if not path.is_file():
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(
                """
                SELECT deployment_id, reason, latched_at
                FROM risk_envelope_rollback_latches
                ORDER BY deployment_id
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    return [
        {
            "deployment_id": str(row[0]),
            "reason": str(row[1]),
            "latched_at": str(row[2]),
        }
        for row in rows
        if deployment_ids is None or str(row[0]) in deployment_ids
    ]


class ExitStateRepository(ABC):
    @abstractmethod
    async def freeze_policy_and_initialize_state(
        self,
        snapshot: TradeExitPolicySnapshot,
        state: ExitRuntimeState,
    ) -> None:
        """Atomically freeze immutable policy and initialize runtime state."""

    @abstractmethod
    async def get_policy_snapshot(
        self, trade_id: str
    ) -> TradeExitPolicySnapshot | None:
        """Return the frozen policy."""

    @abstractmethod
    async def get_runtime_state(self, trade_id: str) -> ExitRuntimeState | None:
        """Return durable runtime state."""

    @abstractmethod
    async def transition_runtime_state(
        self,
        state: ExitRuntimeState,
        *,
        expected_version: int,
    ) -> None:
        """Commit a monotonic transition using optimistic state versioning."""

    @abstractmethod
    async def prepare_action_intent(self, intent: ExitActionIntent) -> None:
        """Persist an idempotent intent before any broker-affecting call."""

    @abstractmethod
    async def bind_action_order(
        self,
        idempotency_key: str,
        *,
        broker_order_id: str,
        broker_payload: dict | None = None,
    ) -> None:
        """Bind broker identity immediately after accepted submission."""

    @abstractmethod
    async def update_action_handoff(
        self,
        idempotency_key: str,
        *,
        handoff_stage: str,
        restore_order_id: str | None = None,
        broker_payload: dict | None = None,
    ) -> None:
        """Advance restart-visible handoff stage and bind restore identity."""

    @abstractmethod
    async def resolve_action_intent(
        self,
        idempotency_key: str,
        *,
        status: str,
        broker_payload: dict | None = None,
    ) -> None:
        """Record a terminal confirmed/failed/abandoned outcome."""

    @abstractmethod
    async def get_open_action_intents(
        self, trade_id: str
    ) -> list[ExitActionIntent]:
        """Return prepared/submitted intents that block duplicate effects."""

    @abstractmethod
    async def latch_risk_envelope_rollback(
        self,
        deployment_id: str,
        *,
        reason: str,
    ) -> None:
        """Durably disable new canary treatment for a deployment."""

    @abstractmethod
    async def get_risk_envelope_rollback(
        self,
        deployment_id: str,
    ) -> dict | None:
        """Return the durable rollback latch, if any."""


class NullExitStateRepository(ExitStateRepository):
    async def freeze_policy_and_initialize_state(
        self,
        snapshot: TradeExitPolicySnapshot,
        state: ExitRuntimeState,
    ) -> None:
        del snapshot, state

    async def get_policy_snapshot(
        self, trade_id: str
    ) -> TradeExitPolicySnapshot | None:
        del trade_id
        return None

    async def get_runtime_state(self, trade_id: str) -> ExitRuntimeState | None:
        del trade_id
        return None

    async def transition_runtime_state(
        self,
        state: ExitRuntimeState,
        *,
        expected_version: int,
    ) -> None:
        del state, expected_version

    async def prepare_action_intent(self, intent: ExitActionIntent) -> None:
        del intent

    async def bind_action_order(
        self,
        idempotency_key: str,
        *,
        broker_order_id: str,
        broker_payload: dict | None = None,
    ) -> None:
        del idempotency_key, broker_order_id, broker_payload

    async def update_action_handoff(
        self,
        idempotency_key: str,
        *,
        handoff_stage: str,
        restore_order_id: str | None = None,
        broker_payload: dict | None = None,
    ) -> None:
        del idempotency_key, handoff_stage, restore_order_id, broker_payload

    async def resolve_action_intent(
        self,
        idempotency_key: str,
        *,
        status: str,
        broker_payload: dict | None = None,
    ) -> None:
        del idempotency_key, status, broker_payload

    async def get_open_action_intents(
        self, trade_id: str
    ) -> list[ExitActionIntent]:
        del trade_id
        return []

    async def latch_risk_envelope_rollback(
        self,
        deployment_id: str,
        *,
        reason: str,
    ) -> None:
        del deployment_id, reason

    async def get_risk_envelope_rollback(
        self,
        deployment_id: str,
    ) -> dict | None:
        del deployment_id
        return None


class SQLiteExitStateRepository(ExitStateRepository):
    def __init__(self, db_path: str, *, backend: SQLiteBackend | None = None) -> None:
        self.backend = backend or SQLiteBackend(db_path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self.backend.run_write(self._init_db)
            self._initialized = True

    async def freeze_policy_and_initialize_state(
        self,
        snapshot: TradeExitPolicySnapshot,
        state: ExitRuntimeState,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._freeze_policy_and_initialize_state_sync,
            snapshot,
            state,
        )

    async def get_policy_snapshot(
        self, trade_id: str
    ) -> TradeExitPolicySnapshot | None:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_policy_snapshot_sync, trade_id)

    async def get_runtime_state(self, trade_id: str) -> ExitRuntimeState | None:
        await self._ensure_initialized()
        return await self.backend.run_read(self._get_runtime_state_sync, trade_id)

    async def transition_runtime_state(
        self,
        state: ExitRuntimeState,
        *,
        expected_version: int,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._transition_runtime_state_sync,
            state,
            expected_version,
        )

    async def prepare_action_intent(self, intent: ExitActionIntent) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(self._prepare_action_intent_sync, intent)

    async def bind_action_order(
        self,
        idempotency_key: str,
        *,
        broker_order_id: str,
        broker_payload: dict | None = None,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._bind_action_order_sync,
            idempotency_key,
            broker_order_id,
            broker_payload,
        )

    async def update_action_handoff(
        self,
        idempotency_key: str,
        *,
        handoff_stage: str,
        restore_order_id: str | None = None,
        broker_payload: dict | None = None,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._update_action_handoff_sync,
            idempotency_key,
            handoff_stage,
            restore_order_id,
            broker_payload,
        )

    async def resolve_action_intent(
        self,
        idempotency_key: str,
        *,
        status: str,
        broker_payload: dict | None = None,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._resolve_action_intent_sync,
            idempotency_key,
            status,
            broker_payload,
        )

    async def get_open_action_intents(
        self, trade_id: str
    ) -> list[ExitActionIntent]:
        await self._ensure_initialized()
        return await self.backend.run_read(
            self._get_open_action_intents_sync,
            trade_id,
        )

    async def latch_risk_envelope_rollback(
        self,
        deployment_id: str,
        *,
        reason: str,
    ) -> None:
        await self._ensure_initialized()
        await self.backend.run_write(
            self._latch_risk_envelope_rollback_sync,
            deployment_id,
            reason,
        )

    async def get_risk_envelope_rollback(
        self,
        deployment_id: str,
    ) -> dict | None:
        await self._ensure_initialized()
        return await self.backend.run_read(
            self._get_risk_envelope_rollback_sync,
            deployment_id,
        )

    def _init_db(self) -> None:
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_exit_policy_snapshots (
                    trade_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    option_symbol TEXT,
                    active_plan_id TEXT,
                    startup_config_id TEXT,
                    policy_schema_version TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    canonical_policy_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    frozen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_exit_runtime_state (
                    trade_id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    option_symbol TEXT,
                    policy_hash TEXT NOT NULL,
                    seed_entry_premium REAL NOT NULL,
                    seed_quantity INTEGER NOT NULL,
                    initial_risk_per_contract REAL NOT NULL,
                    raw_peak_premium REAL NOT NULL,
                    confirmed_peak_r REAL NOT NULL,
                    peak_timestamp TEXT,
                    locked_floor_r REAL,
                    committed_stop_price REAL,
                    target_1_banked INTEGER NOT NULL DEFAULT 0,
                    banked_quantity INTEGER NOT NULL DEFAULT 0,
                    breakeven_emitted INTEGER NOT NULL DEFAULT 0,
                    runner_state TEXT NOT NULL DEFAULT 'pre_t1',
                    recovery_status TEXT NOT NULL DEFAULT 'active',
                    degraded_reason TEXT,
                    last_evaluated_at TEXT,
                    state_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exit_action_intents (
                    idempotency_key TEXT PRIMARY KEY,
                    trade_id TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    action_slot TEXT NOT NULL,
                    expected_state_version INTEGER NOT NULL,
                    requested_quantity INTEGER,
                    requested_stop_price REAL,
                    requested_floor_r REAL,
                    prior_stop_order_id TEXT,
                    prior_stop_price REAL,
                    handoff_stage TEXT NOT NULL DEFAULT 'prepared',
                    status TEXT NOT NULL,
                    broker_order_id TEXT,
                    restore_order_id TEXT,
                    broker_payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(trade_id, action_slot)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exit_action_intents_trade "
                "ON exit_action_intents(trade_id, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_envelope_rollback_latches (
                    deployment_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    latched_at TEXT NOT NULL
                )
                """
            )
            intent_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(exit_action_intents)")
            }
            for column, declaration in (
                ("requested_floor_r", "REAL"),
                ("prior_stop_order_id", "TEXT"),
                ("prior_stop_price", "REAL"),
                ("handoff_stage", "TEXT NOT NULL DEFAULT 'prepared'"),
                ("restore_order_id", "TEXT"),
            ):
                if column not in intent_columns:
                    conn.execute(
                        f"ALTER TABLE exit_action_intents "
                        f"ADD COLUMN {column} {declaration}"
                    )
            conn.commit()

    def _latch_risk_envelope_rollback_sync(
        self,
        deployment_id: str,
        reason: str,
    ) -> None:
        if not deployment_id.strip() or not reason.strip():
            raise ValueError("deployment_id and rollback reason are required")
        now = datetime.now(UTC).isoformat()
        with closing(self.backend.connect()) as conn:
            conn.execute(
                """
                INSERT INTO risk_envelope_rollback_latches (
                    deployment_id, reason, latched_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(deployment_id) DO NOTHING
                """,
                (deployment_id, reason, now),
            )
            conn.commit()

    def _get_risk_envelope_rollback_sync(
        self,
        deployment_id: str,
    ) -> dict | None:
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                """
                SELECT deployment_id, reason, latched_at
                FROM risk_envelope_rollback_latches
                WHERE deployment_id=?
                """,
                (deployment_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "deployment_id": row[0],
            "reason": row[1],
            "latched_at": row[2],
        }

    def _freeze_policy_and_initialize_state_sync(
        self,
        snapshot: TradeExitPolicySnapshot,
        state: ExitRuntimeState,
    ) -> None:
        _validate_snapshot_state_identity(snapshot, state)
        if canonical_policy_hash(snapshot.canonical_policy) != snapshot.policy_hash:
            raise ValueError("frozen policy hash does not match canonical policy")
        # Persist the complete frozen snapshot, including non-semantic
        # operator/provenance labels. Identity validation above intentionally
        # hashes only executable fields.
        policy_json = json.dumps(
            snapshot.canonical_policy,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        provenance_json = json.dumps(
            snapshot.provenance,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        now = datetime.now(UTC).isoformat()
        frozen_at = snapshot.frozen_at.isoformat() if snapshot.frozen_at else now
        with closing(self.backend.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_policy = conn.execute(
                """
                SELECT deployment_id, option_symbol, active_plan_id,
                       startup_config_id, policy_schema_version, policy_id,
                       policy_hash, canonical_policy_json, provenance_json
                FROM trade_exit_policy_snapshots WHERE trade_id = ?
                """,
                (snapshot.trade_id,),
            ).fetchone()
            proposed_policy = (
                snapshot.deployment_id,
                snapshot.option_symbol,
                snapshot.active_plan_id,
                snapshot.startup_config_id,
                snapshot.policy_schema_version,
                snapshot.policy_id,
                snapshot.policy_hash,
                policy_json,
                provenance_json,
            )
            if existing_policy is not None and tuple(existing_policy) != proposed_policy:
                conn.rollback()
                raise ValueError(
                    f"trade {snapshot.trade_id!r} frozen policy identity conflict"
                )
            if existing_policy is None:
                conn.execute(
                    """
                    INSERT INTO trade_exit_policy_snapshots (
                        trade_id, deployment_id, option_symbol, active_plan_id,
                        startup_config_id, policy_schema_version, policy_id,
                        policy_hash, canonical_policy_json, provenance_json,
                        frozen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (snapshot.trade_id, *proposed_policy, frozen_at),
                )
            existing_state = conn.execute(
                "SELECT policy_hash FROM trade_exit_runtime_state WHERE trade_id = ?",
                (state.trade_id,),
            ).fetchone()
            if existing_state is not None and existing_state[0] != state.policy_hash:
                conn.rollback()
                raise ValueError(
                    f"trade {state.trade_id!r} runtime policy hash conflict"
                )
            if existing_state is None:
                self._insert_state(conn, state, now)
            conn.commit()

    def _transition_runtime_state_sync(
        self,
        state: ExitRuntimeState,
        expected_version: int,
    ) -> None:
        _validate_runtime_state(state)
        if state.state_version != expected_version + 1:
            raise ValueError("state_version must increment exactly once")
        now = datetime.now(UTC).isoformat()
        with closing(self.backend.connect()) as conn:
            current = conn.execute(
                """
                SELECT deployment_id, option_symbol, policy_hash,
                       seed_entry_premium, seed_quantity,
                       raw_peak_premium, confirmed_peak_r, locked_floor_r,
                       committed_stop_price, target_1_banked, banked_quantity,
                       breakeven_emitted, state_version
                FROM trade_exit_runtime_state WHERE trade_id = ?
                """,
                (state.trade_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"trade {state.trade_id!r} runtime state is missing")
            if current[12] != expected_version:
                raise ValueError(
                    f"trade {state.trade_id!r} state version conflict: "
                    f"expected {expected_version}, found {current[12]}"
                )
            identity = (
                state.deployment_id,
                state.option_symbol,
                state.policy_hash,
                state.seed_entry_premium,
                state.seed_quantity,
            )
            if tuple(current[:5]) != identity:
                raise ValueError(
                    f"trade {state.trade_id!r} runtime state identity conflict"
                )
            _require_non_regression(current, state)
            result = conn.execute(
                """
                UPDATE trade_exit_runtime_state SET
                    raw_peak_premium = ?,
                    confirmed_peak_r = ?,
                    peak_timestamp = ?,
                    locked_floor_r = ?,
                    committed_stop_price = ?,
                    target_1_banked = ?,
                    banked_quantity = ?,
                    breakeven_emitted = ?,
                    runner_state = ?,
                    recovery_status = ?,
                    degraded_reason = ?,
                    last_evaluated_at = ?,
                    state_version = ?,
                    updated_at = ?
                WHERE trade_id = ? AND state_version = ?
                """,
                (
                    state.raw_peak_premium,
                    state.confirmed_peak_r,
                    state.peak_timestamp.isoformat() if state.peak_timestamp else None,
                    state.locked_floor_r,
                    state.committed_stop_price,
                    int(state.target_1_banked),
                    state.banked_quantity,
                    int(state.breakeven_emitted),
                    state.runner_state,
                    state.recovery_status,
                    state.degraded_reason,
                    state.last_evaluated_at.isoformat()
                    if state.last_evaluated_at
                    else None,
                    state.state_version,
                    now,
                    state.trade_id,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                conn.rollback()
                raise ValueError(f"trade {state.trade_id!r} state transition lost race")
            conn.commit()

    def _insert_state(self, conn, state: ExitRuntimeState, now: str) -> None:
        _validate_runtime_state(state)
        conn.execute(
            """
            INSERT INTO trade_exit_runtime_state (
                trade_id, deployment_id, option_symbol, policy_hash,
                seed_entry_premium, seed_quantity, initial_risk_per_contract,
                raw_peak_premium, confirmed_peak_r, peak_timestamp,
                locked_floor_r, committed_stop_price, target_1_banked,
                banked_quantity, breakeven_emitted, runner_state,
                recovery_status, degraded_reason, last_evaluated_at,
                state_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.trade_id,
                state.deployment_id,
                state.option_symbol,
                state.policy_hash,
                state.seed_entry_premium,
                state.seed_quantity,
                state.initial_risk_per_contract,
                state.raw_peak_premium,
                state.confirmed_peak_r,
                state.peak_timestamp.isoformat() if state.peak_timestamp else None,
                state.locked_floor_r,
                state.committed_stop_price,
                int(state.target_1_banked),
                state.banked_quantity,
                int(state.breakeven_emitted),
                state.runner_state,
                state.recovery_status,
                state.degraded_reason,
                state.last_evaluated_at.isoformat()
                if state.last_evaluated_at
                else None,
                state.state_version,
                now,
            ),
        )

    def _prepare_action_intent_sync(self, intent: ExitActionIntent) -> None:
        if intent.status != "prepared":
            raise ValueError("new action intent must start prepared")
        now = datetime.now(UTC).isoformat()
        created_at = intent.created_at.isoformat() if intent.created_at else now
        with closing(self.backend.connect()) as conn:
            runtime = conn.execute(
                "SELECT policy_hash, state_version FROM trade_exit_runtime_state "
                "WHERE trade_id = ?",
                (intent.trade_id,),
            ).fetchone()
            if runtime is None:
                raise ValueError(
                    f"trade {intent.trade_id!r} runtime state is missing"
                )
            if runtime[0] != intent.policy_hash:
                raise ValueError("exit action intent policy hash conflict")
            if int(runtime[1]) != intent.expected_state_version:
                raise ValueError("exit action intent state version conflict")
            try:
                conn.execute(
                    """
                    INSERT INTO exit_action_intents (
                        idempotency_key, trade_id, policy_hash, action_kind,
                        action_slot, expected_state_version, requested_quantity,
                        requested_stop_price, requested_floor_r,
                        prior_stop_order_id, prior_stop_price,
                        handoff_stage, status, broker_order_id, restore_order_id,
                        broker_payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        intent.idempotency_key,
                        intent.trade_id,
                        intent.policy_hash,
                        intent.action_kind,
                        intent.action_slot,
                        intent.expected_state_version,
                        intent.requested_quantity,
                        intent.requested_stop_price,
                        intent.requested_floor_r,
                        intent.prior_stop_order_id,
                        intent.prior_stop_price,
                        intent.handoff_stage,
                        created_at,
                        now,
                    ),
                )
            except Exception:
                existing = conn.execute(
                    """
                    SELECT idempotency_key, policy_hash, action_kind,
                           expected_state_version, requested_quantity,
                           requested_stop_price, requested_floor_r,
                           prior_stop_order_id, prior_stop_price, handoff_stage
                    FROM exit_action_intents
                    WHERE trade_id = ? AND action_slot = ?
                    """,
                    (intent.trade_id, intent.action_slot),
                ).fetchone()
                expected = (
                    intent.idempotency_key,
                    intent.policy_hash,
                    intent.action_kind,
                    intent.expected_state_version,
                    intent.requested_quantity,
                    intent.requested_stop_price,
                    intent.requested_floor_r,
                    intent.prior_stop_order_id,
                    intent.prior_stop_price,
                    intent.handoff_stage,
                )
                if existing is None or tuple(existing) != expected:
                    raise ValueError(
                        f"trade {intent.trade_id!r} action slot "
                        f"{intent.action_slot!r} already has a different intent"
                    )
            conn.commit()

    def _update_action_handoff_sync(
        self,
        idempotency_key: str,
        handoff_stage: str,
        restore_order_id: str | None,
        broker_payload: dict | None,
    ) -> None:
        if not handoff_stage.strip():
            raise ValueError("handoff_stage is required")
        now = datetime.now(UTC).isoformat()
        payload = (
            json.dumps(broker_payload, default=str, sort_keys=True)
            if broker_payload is not None
            else None
        )
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                "SELECT status, restore_order_id, handoff_stage FROM exit_action_intents "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown exit action intent {idempotency_key!r}")
            if row[0] not in {"prepared", "submitted"}:
                raise ValueError(f"cannot update terminal exit action intent {row[0]!r}")
            if (
                row[1] is not None
                and restore_order_id is not None
                and row[1] != restore_order_id
                and row[2] != "restore_submit_pending"
            ):
                raise ValueError("exit action intent restore order identity conflict")
            conn.execute(
                """
                UPDATE exit_action_intents
                SET handoff_stage=?,
                    restore_order_id=CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE restore_order_id
                    END,
                    broker_payload_json=COALESCE(?, broker_payload_json),
                    updated_at=?
                WHERE idempotency_key=?
                """,
                (
                    handoff_stage,
                    restore_order_id,
                    restore_order_id,
                    payload,
                    now,
                    idempotency_key,
                ),
            )
            conn.commit()

    def _bind_action_order_sync(
        self,
        idempotency_key: str,
        broker_order_id: str,
        broker_payload: dict | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        payload = (
            json.dumps(broker_payload, default=str, sort_keys=True)
            if broker_payload is not None
            else None
        )
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                "SELECT status, broker_order_id FROM exit_action_intents "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown exit action intent {idempotency_key!r}")
            if row[1] is not None and row[1] != broker_order_id:
                raise ValueError("exit action intent broker order identity conflict")
            if row[0] not in {"prepared", "submitted"}:
                raise ValueError(f"cannot bind terminal exit action intent {row[0]!r}")
            conn.execute(
                """
                UPDATE exit_action_intents
                SET status='submitted', broker_order_id=?,
                    broker_payload_json=COALESCE(?, broker_payload_json),
                    updated_at=?
                WHERE idempotency_key=?
                """,
                (broker_order_id, payload, now, idempotency_key),
            )
            conn.commit()

    def _resolve_action_intent_sync(
        self,
        idempotency_key: str,
        status: str,
        broker_payload: dict | None,
    ) -> None:
        if status not in {"confirmed", "failed", "abandoned"}:
            raise ValueError("invalid terminal action intent status")
        now = datetime.now(UTC).isoformat()
        payload = (
            json.dumps(broker_payload, default=str, sort_keys=True)
            if broker_payload is not None
            else None
        )
        with closing(self.backend.connect()) as conn:
            result = conn.execute(
                """
                UPDATE exit_action_intents
                SET status=?, broker_payload_json=COALESCE(?, broker_payload_json),
                    updated_at=?
                WHERE idempotency_key=?
                """,
                (status, payload, now, idempotency_key),
            )
            if result.rowcount != 1:
                raise ValueError(f"unknown exit action intent {idempotency_key!r}")
            conn.commit()

    def _get_open_action_intents_sync(
        self, trade_id: str
    ) -> list[ExitActionIntent]:
        with closing(self.backend.connect()) as conn:
            rows = conn.execute(
                """
                SELECT idempotency_key, trade_id, policy_hash, action_kind,
                       action_slot, expected_state_version, requested_quantity,
                       requested_stop_price, requested_floor_r,
                       prior_stop_order_id, prior_stop_price,
                       handoff_stage, status, broker_order_id, restore_order_id,
                       broker_payload_json, created_at, updated_at
                FROM exit_action_intents
                WHERE trade_id=? AND status IN ('prepared', 'submitted')
                ORDER BY created_at, idempotency_key
                """,
                (trade_id,),
            ).fetchall()
        return [_intent_from_row(row) for row in rows]

    def _get_policy_snapshot_sync(
        self, trade_id: str
    ) -> TradeExitPolicySnapshot | None:
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                """
                SELECT trade_id, deployment_id, option_symbol, active_plan_id,
                       startup_config_id, policy_schema_version, policy_id,
                       policy_hash, canonical_policy_json, provenance_json,
                       frozen_at
                FROM trade_exit_policy_snapshots WHERE trade_id=?
                """,
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return TradeExitPolicySnapshot(
            trade_id=row[0],
            deployment_id=row[1],
            option_symbol=row[2],
            active_plan_id=row[3],
            startup_config_id=row[4],
            policy_schema_version=row[5],
            policy_id=row[6],
            policy_hash=row[7],
            canonical_policy=json.loads(row[8]),
            provenance=json.loads(row[9]),
            frozen_at=datetime.fromisoformat(row[10]),
        )

    def _get_runtime_state_sync(self, trade_id: str) -> ExitRuntimeState | None:
        with closing(self.backend.connect()) as conn:
            row = conn.execute(
                """
                SELECT trade_id, deployment_id, option_symbol, policy_hash,
                       seed_entry_premium, seed_quantity,
                       initial_risk_per_contract, raw_peak_premium,
                       confirmed_peak_r, peak_timestamp, locked_floor_r,
                       committed_stop_price, target_1_banked, banked_quantity,
                       breakeven_emitted, runner_state, recovery_status,
                       degraded_reason, last_evaluated_at, state_version
                FROM trade_exit_runtime_state WHERE trade_id=?
                """,
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return ExitRuntimeState(
            trade_id=row[0],
            deployment_id=row[1],
            option_symbol=row[2],
            policy_hash=row[3],
            seed_entry_premium=float(row[4]),
            seed_quantity=int(row[5]),
            initial_risk_per_contract=float(row[6]),
            raw_peak_premium=float(row[7]),
            confirmed_peak_r=float(row[8]),
            peak_timestamp=datetime.fromisoformat(row[9]) if row[9] else None,
            locked_floor_r=float(row[10]) if row[10] is not None else None,
            committed_stop_price=float(row[11]) if row[11] is not None else None,
            target_1_banked=bool(row[12]),
            banked_quantity=int(row[13]),
            breakeven_emitted=bool(row[14]),
            runner_state=row[15],
            recovery_status=row[16],
            degraded_reason=row[17],
            last_evaluated_at=datetime.fromisoformat(row[18]) if row[18] else None,
            state_version=int(row[19]),
        )


def _validate_snapshot_state_identity(
    snapshot: TradeExitPolicySnapshot,
    state: ExitRuntimeState,
) -> None:
    if (
        snapshot.trade_id != state.trade_id
        or snapshot.deployment_id != state.deployment_id
        or snapshot.option_symbol != state.option_symbol
        or snapshot.policy_hash != state.policy_hash
    ):
        raise ValueError("frozen policy and runtime state identity mismatch")


def _validate_runtime_state(state: ExitRuntimeState) -> None:
    if state.state_version < 1:
        raise ValueError("state_version must be >= 1")
    if state.seed_entry_premium <= 0 or state.seed_quantity <= 0:
        raise ValueError("entry premium and seed quantity must be positive")
    if state.initial_risk_per_contract <= 0:
        raise ValueError("initial risk per contract must be positive")
    if state.raw_peak_premium < state.seed_entry_premium:
        raise ValueError("raw peak cannot be below seed entry premium")
    if state.confirmed_peak_r < 0:
        raise ValueError("confirmed peak R cannot be negative")
    if state.banked_quantity < 0 or state.banked_quantity > state.seed_quantity:
        raise ValueError("banked quantity must stay within original quantity")


def _require_non_regression(current, state: ExitRuntimeState) -> None:
    if state.raw_peak_premium < float(current[5]):
        raise ValueError("raw peak premium cannot regress")
    if state.confirmed_peak_r < float(current[6]):
        raise ValueError("confirmed peak R cannot regress")
    if (
        current[7] is not None
        and (state.locked_floor_r is None or state.locked_floor_r < float(current[7]))
    ):
        raise ValueError("locked floor R cannot regress")
    if (
        current[8] is not None
        and (
            state.committed_stop_price is None
            or state.committed_stop_price < float(current[8])
        )
    ):
        raise ValueError("committed stop price cannot regress")
    if bool(current[9]) and not state.target_1_banked:
        raise ValueError("target_1_banked cannot regress")
    if state.banked_quantity < int(current[10]):
        raise ValueError("banked quantity cannot regress")
    if bool(current[11]) and not state.breakeven_emitted:
        raise ValueError("breakeven state cannot regress")


def _intent_from_row(row) -> ExitActionIntent:
    return ExitActionIntent(
        idempotency_key=row[0],
        trade_id=row[1],
        policy_hash=row[2],
        action_kind=row[3],
        action_slot=row[4],
        expected_state_version=int(row[5]),
        requested_quantity=int(row[6]) if row[6] is not None else None,
        requested_stop_price=float(row[7]) if row[7] is not None else None,
        requested_floor_r=float(row[8]) if row[8] is not None else None,
        prior_stop_order_id=row[9],
        prior_stop_price=float(row[10]) if row[10] is not None else None,
        handoff_stage=row[11],
        status=row[12],
        broker_order_id=row[13],
        restore_order_id=row[14],
        broker_payload=json.loads(row[15]) if row[15] else None,
        created_at=datetime.fromisoformat(row[16]),
        updated_at=datetime.fromisoformat(row[17]),
    )
