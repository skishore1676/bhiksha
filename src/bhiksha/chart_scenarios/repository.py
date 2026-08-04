"""Experiment-specific SQLite state and hash-linked shadow event storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping

from mala_bhiksha_kernel import ChartScenarioSpec, ScenarioShadowEvent, ShadowEventType, canonical_sha256

from .models import as_utc, timestamp_json


class IdempotencyConflict(RuntimeError):
    """The same event identity was replayed with different immutable facts."""


class TerminalScenarioError(RuntimeError):
    """A caller attempted to arm a scenario after its terminal event."""


@dataclass(frozen=True, slots=True)
class EventWrite:
    event: ScenarioShadowEvent
    created: bool
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class EventChainReport:
    valid: bool
    event_count: int
    last_event_hash: str | None
    errors: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.valid


def scenario_identity_key(scenario: ChartScenarioSpec, trigger_version: str) -> tuple[str, str, str, str, str]:
    return (
        scenario.campaign_id,
        scenario.run_id,
        scenario.arm_id.value,
        scenario.scenario_id,
        trigger_version,
    )


def _event_role_key(scenario: ChartScenarioSpec, trigger_version: str, role: str) -> str:
    fields = scenario_identity_key(scenario, trigger_version)
    if not role.strip():
        raise ValueError("event idempotency role must be non-empty")
    return canonical_sha256({"identity": fields, "role": role})


class ScenarioEventRepository:
    """A narrow repository for one experiment database.

    Writes use a short lock budget and fail visibly.  Status/readback uses a
    separate bounded budget so a brief schema/writer lock does not become an
    unbounded wait while quote observation remains latency-sensitive.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        write_busy_timeout_ms: int = 1,
        read_busy_timeout_ms: int = 250,
    ) -> None:
        self.db_path = Path(db_path)
        self.write_busy_timeout_ms = int(write_busy_timeout_ms)
        self.read_busy_timeout_ms = int(read_busy_timeout_ms)
        if self.write_busy_timeout_ms < 1 or self.read_busy_timeout_ms < 1:
            raise ValueError("SQLite lock budgets must be positive")
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        timeout = (self.write_busy_timeout_ms if write else self.read_busy_timeout_ms) / 1000.0
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.write_busy_timeout_ms if write else self.read_busy_timeout_ms}")
        if write:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect(write=True) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS chart_scenario_states (
                        campaign_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        arm_id TEXT NOT NULL,
                        scenario_id TEXT NOT NULL,
                        trigger_version TEXT NOT NULL,
                        scenario_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        last_event_hash TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (campaign_id, run_id, arm_id, scenario_id, trigger_version)
                    );
                    CREATE TABLE IF NOT EXISTS chart_scenario_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        campaign_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        arm_id TEXT NOT NULL,
                        scenario_id TEXT NOT NULL,
                        trigger_version TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_hash TEXT NOT NULL,
                        preceding_event_hash TEXT,
                        payload_fingerprint TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS chart_scenario_events_identity_idx
                        ON chart_scenario_events (campaign_id, run_id, arm_id, scenario_id, trigger_version);
                    """
                )
            self._schema_ready = True

    def register_scenario(self, scenario: ChartScenarioSpec, trigger_version: str) -> bool:
        """Register a scenario identity; return ``True`` only on first insert."""

        self.ensure_schema()
        campaign_id, run_id, arm_id, scenario_id, version = scenario_identity_key(scenario, trigger_version)
        now = timestamp_json(datetime.now(UTC))
        scenario_json = json.dumps(scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT scenario_hash FROM chart_scenario_states
                WHERE campaign_id=? AND run_id=? AND arm_id=? AND scenario_id=? AND trigger_version=?
                """,
                (campaign_id, run_id, arm_id, scenario_id, version),
            ).fetchone()
            if row is not None:
                if row["scenario_hash"] != scenario.scenario_hash:
                    raise ValueError("scenario identity was reused with a different scenario hash")
                return False
            conn.execute(
                """
                INSERT INTO chart_scenario_states
                (campaign_id, run_id, arm_id, scenario_id, trigger_version, scenario_hash,
                 status, terminal, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'installed', 0, ?, ?)
                """,
                (
                    campaign_id,
                    run_id,
                    arm_id,
                    scenario_id,
                    version,
                    scenario.scenario_hash,
                    json.dumps({"scenario": json.loads(scenario_json)}, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
        return True

    def get_state(
        self,
        scenario: ChartScenarioSpec,
        trigger_version: str,
    ) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        if not self._tables_exist_readonly():
            return None
        key = scenario_identity_key(scenario, trigger_version)
        with self._connect(write=False) as conn:
            row = conn.execute(
                """
                SELECT * FROM chart_scenario_states
                WHERE campaign_id=? AND run_id=? AND arm_id=? AND scenario_id=? AND trigger_version=?
                """,
                key,
            ).fetchone()
        if row is None:
            return None
        metadata = json.loads(row["metadata_json"] or "{}")
        return {
            "campaign_id": row["campaign_id"],
            "run_id": row["run_id"],
            "arm_id": row["arm_id"],
            "scenario_id": row["scenario_id"],
            "trigger_version": row["trigger_version"],
            "scenario_hash": row["scenario_hash"],
            "status": row["status"],
            "terminal": bool(row["terminal"]),
            "last_event_hash": row["last_event_hash"],
            "updated_at": row["updated_at"],
            **metadata,
        }

    def append_event(
        self,
        *,
        scenario: ChartScenarioSpec,
        trigger_version: str,
        event_type: ShadowEventType | str,
        event_time: datetime | str,
        market_observation_id: str,
        details: Mapping[str, Any],
        role: str,
        state_updates: Mapping[str, Any] | None = None,
        terminal: bool = False,
    ) -> EventWrite:
        """Append one kernel event and state transition in one SQLite transaction."""

        self.ensure_schema()
        try:
            kind = event_type if isinstance(event_type, ShadowEventType) else ShadowEventType(event_type)
        except ValueError as exc:
            raise ValueError(f"unknown shadow event type: {event_type!r}") from exc
        if not market_observation_id.strip():
            raise ValueError("market_observation_id must be non-empty")
        identity = scenario_identity_key(scenario, trigger_version)
        idempotency_key = _event_role_key(scenario, trigger_version, role)
        updates = dict(state_updates or {})
        fingerprint = canonical_sha256(
            {
                "event_type": kind.value,
                "event_time": timestamp_json(as_utc(event_time)),
                "market_observation_id": market_observation_id,
                "details": details,
                "state_updates": updates,
                "terminal": terminal,
            }
        )
        with self._connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT event_json, payload_fingerprint FROM chart_scenario_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("event identity was replayed with different facts")
                event = ScenarioShadowEvent.model_validate(json.loads(existing["event_json"]))
                return EventWrite(event=event, created=False, idempotency_key=idempotency_key)

            state_row = conn.execute(
                """
                SELECT status, terminal, metadata_json FROM chart_scenario_states
                WHERE campaign_id=? AND run_id=? AND arm_id=? AND scenario_id=? AND trigger_version=?
                """,
                identity,
            ).fetchone()
            if state_row is None:
                raise ValueError("scenario must be registered before appending events")
            if bool(state_row["terminal"]):
                raise TerminalScenarioError("terminal scenario cannot accept a new event")

            previous_row = conn.execute(
                "SELECT event_hash FROM chart_scenario_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            preceding_hash = previous_row["event_hash"] if previous_row is not None else None
            event_id = "event-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:48]
            event = ScenarioShadowEvent.model_validate(
                {
                    "schema_version": "market-context-shadow-event.v1",
                    "program_id": scenario.program_id,
                    "experiment_family_id": scenario.experiment_family_id,
                    "experiment_version": scenario.experiment_version,
                    "campaign_id": scenario.campaign_id,
                    "run_id": scenario.run_id,
                    "arm_id": scenario.arm_id.value,
                    "scenario_id": scenario.scenario_id,
                    "event_id": event_id,
                    "event_type": kind.value,
                    "event_time": timestamp_json(as_utc(event_time)),
                    "market_observation_id": market_observation_id,
                    "scenario_hash": scenario.scenario_hash,
                    "implementation_hash": scenario.component_manifest_hash,
                    "preceding_event_hash": preceding_hash,
                    "details": dict(details),
                    "broker_effect_count": 0,
                    "authorization_mode": scenario.authorization_mode.value,
                    "source_type": scenario.source_type.value,
                }
            )
            event_json = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO chart_scenario_events
                (event_id, idempotency_key, campaign_id, run_id, arm_id, scenario_id,
                 trigger_version, event_type, event_hash, preceding_event_hash,
                 payload_fingerprint, event_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    idempotency_key,
                    *identity,
                    kind.value,
                    event.event_hash,
                    preceding_hash,
                    fingerprint,
                    event_json,
                    timestamp_json(as_utc(event_time)),
                ),
            )
            prior_metadata = json.loads(state_row["metadata_json"] or "{}")
            prior_metadata.update(updates)
            prior_metadata["last_event_type"] = kind.value
            prior_metadata["last_event_observation_id"] = market_observation_id
            status = str(updates.get("status", state_row["status"]))
            terminal_value = bool(state_row["terminal"]) or terminal or bool(updates.get("terminal", False))
            conn.execute(
                """
                UPDATE chart_scenario_states
                SET status=?, terminal=?, metadata_json=?, last_event_hash=?, updated_at=?
                WHERE campaign_id=? AND run_id=? AND arm_id=? AND scenario_id=? AND trigger_version=?
                """,
                (
                    status,
                    int(terminal_value),
                    json.dumps(prior_metadata, sort_keys=True, separators=(",", ":")),
                    event.event_hash,
                    timestamp_json(as_utc(event_time)),
                    *identity,
                ),
            )
        return EventWrite(event=event, created=True, idempotency_key=idempotency_key)

    def events(self) -> list[ScenarioShadowEvent]:
        if not self.db_path.exists() or not self._tables_exist_readonly():
            return []
        with self._connect(write=False) as conn:
            rows = conn.execute("SELECT event_json FROM chart_scenario_events ORDER BY sequence").fetchall()
        return [ScenarioShadowEvent.model_validate(json.loads(row["event_json"])) for row in rows]

    def event_count(self) -> int:
        if not self.db_path.exists() or not self._tables_exist_readonly():
            return 0
        with self._connect(write=False) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM chart_scenario_events").fetchone()[0])

    def verify_event_chain(self) -> EventChainReport:
        if not self.db_path.exists() or not self._tables_exist_readonly():
            return EventChainReport(valid=True, event_count=0, last_event_hash=None)
        errors: list[str] = []
        with self._connect(write=False) as conn:
            rows = conn.execute(
                "SELECT sequence, event_hash, preceding_event_hash, event_json FROM chart_scenario_events ORDER BY sequence"
            ).fetchall()
        previous: str | None = None
        for row in rows:
            try:
                event = ScenarioShadowEvent.model_validate(json.loads(row["event_json"]))
                if event.event_hash != row["event_hash"]:
                    errors.append(f"sequence {row['sequence']} stored event hash mismatch")
                if event.preceding_event_hash != previous:
                    errors.append(f"sequence {row['sequence']} predecessor mismatch")
                previous = event.event_hash
            except Exception as exc:
                errors.append(f"sequence {row['sequence']} invalid event: {exc}")
                previous = row["event_hash"]
        return EventChainReport(
            valid=not errors,
            event_count=len(rows),
            last_event_hash=previous,
            errors=tuple(errors),
        )

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists() or not self._tables_exist_readonly():
            return {
                "db_path": str(self.db_path),
                "scenario_count": 0,
                "terminal_scenario_count": 0,
                "event_count": 0,
                "broker_effect_count": 0,
                "event_chain_valid": True,
            }
        with self._connect(write=False) as conn:
            scenario_count = int(conn.execute("SELECT COUNT(*) FROM chart_scenario_states").fetchone()[0])
            terminal_count = int(conn.execute("SELECT COUNT(*) FROM chart_scenario_states WHERE terminal=1").fetchone()[0])
            event_count = int(conn.execute("SELECT COUNT(*) FROM chart_scenario_events").fetchone()[0])
            last = conn.execute("SELECT event_hash FROM chart_scenario_events ORDER BY sequence DESC LIMIT 1").fetchone()
        chain = self.verify_event_chain()
        return {
            "db_path": str(self.db_path),
            "scenario_count": scenario_count,
            "terminal_scenario_count": terminal_count,
            "event_count": event_count,
            "last_event_hash": last["event_hash"] if last is not None else None,
            "event_chain_valid": chain.valid,
            "event_chain_errors": list(chain.errors),
            "broker_effect_count": 0,
        }

    def _tables_exist_readonly(self) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect(write=False) as conn:
            names = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        return {"chart_scenario_states", "chart_scenario_events"}.issubset(names)


__all__ = [
    "EventChainReport",
    "EventWrite",
    "IdempotencyConflict",
    "ScenarioEventRepository",
    "SQLiteScenarioRepository",
    "TerminalScenarioError",
    "scenario_identity_key",
]


SQLiteScenarioRepository = ScenarioEventRepository
