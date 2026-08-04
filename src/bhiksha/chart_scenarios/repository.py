"""Experiment-specific SQLite state and hash-linked shadow event storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import (
    ChartScenarioSpec,
    ScenarioShadowEvent,
    ShadowEventType,
    canonical_sha256,
)

from .models import as_utc, timestamp_json
from .paths import require_experiment_path


class IdempotencyConflict(RuntimeError):
    """The same event identity was replayed with different immutable facts."""


class TerminalScenarioError(RuntimeError):
    """A caller attempted to arm a scenario after its terminal event."""


OBSERVATION_SLOT_SCHEMA = "market-context-observation-slot.v1"


def canonical_observation_slot_id(*, run_manifest_hash: str, ordinal: int) -> str:
    """Return the run-owned identity for one market-observation cycle."""

    if ordinal < 1:
        raise ValueError("observation slot ordinal must be positive")
    normalized = str(run_manifest_hash).removeprefix("sha256:")
    if len(normalized) != 64:
        raise ValueError("run_manifest_hash must be a sha256 identity")
    digest = canonical_sha256(
        {
            "schema": OBSERVATION_SLOT_SCHEMA,
            "run_manifest_hash": normalized,
            "ordinal": ordinal,
        }
    )
    return "observation-slot-" + digest[:32]


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


def scenario_identity_key(
    scenario: ChartScenarioSpec, trigger_version: str
) -> tuple[str, str, str, str, str]:
    return (
        scenario.campaign_id,
        scenario.run_id,
        scenario.arm_id.value,
        scenario.scenario_id,
        trigger_version,
    )


def _event_role_key(
    scenario: ChartScenarioSpec, trigger_version: str, role: str
) -> str:
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
        self.db_path = require_experiment_path(db_path, role="database")
        self.write_busy_timeout_ms = int(write_busy_timeout_ms)
        self.read_busy_timeout_ms = int(read_busy_timeout_ms)
        if self.write_busy_timeout_ms < 1 or self.read_busy_timeout_ms < 1:
            raise ValueError("SQLite lock budgets must be positive")
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        timeout = (
            self.write_busy_timeout_ms if write else self.read_busy_timeout_ms
        ) / 1000.0
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"PRAGMA busy_timeout={self.write_busy_timeout_ms if write else self.read_busy_timeout_ms}"
        )
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
                        plan_hash TEXT NOT NULL,
                        policy_registry_hash TEXT NOT NULL,
                        treatment_hash TEXT NOT NULL,
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
                    CREATE TABLE IF NOT EXISTS chart_scenario_market_facts (
                        campaign_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        candidate_id TEXT NOT NULL,
                        observation_key TEXT NOT NULL,
                        facts_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (
                            campaign_id, run_id, candidate_id, observation_key
                        )
                    );
                    CREATE TABLE IF NOT EXISTS chart_scenario_observation_slots (
                        campaign_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        candidate_id TEXT NOT NULL,
                        slot_ordinal INTEGER NOT NULL,
                        slot_id TEXT NOT NULL,
                        plan_hash TEXT NOT NULL,
                        run_manifest_hash TEXT NOT NULL,
                        treatment_manifest_hash TEXT NOT NULL,
                        run_input_hashes_hash TEXT NOT NULL,
                        cartographer_receipt_hash TEXT NOT NULL,
                        cartographer_export_hash TEXT NOT NULL,
                        observation_window_hash TEXT NOT NULL,
                        facts_hash TEXT NOT NULL,
                        evaluated_at TEXT NOT NULL,
                        observed_arm_ids_json TEXT NOT NULL,
                        expected_arm_ids_json TEXT NOT NULL,
                        paired INTEGER NOT NULL DEFAULT 0,
                        proof_hash TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (
                            campaign_id, run_id, candidate_id, slot_ordinal
                        ),
                        UNIQUE (campaign_id, run_id, candidate_id, slot_id)
                    );
                    """
                )
            self._schema_ready = True

    def bind_market_facts(
        self,
        scenario: ChartScenarioSpec,
        *,
        observation_slot_ordinal: int,
        run_manifest_hash: str,
        plan_hash: str,
        treatment_manifest_hash: str,
        run_input_hashes_hash: str,
        cartographer_receipt_hash: str,
        cartographer_export_hash: str,
        observation_window_hash: str,
        expected_arm_ids: tuple[str, ...],
        facts_hash: str,
        evaluated_at: str,
    ) -> dict[str, Any]:
        """Bind one run-owned slot and persist a proof when all arms agree."""

        self.ensure_schema()
        normalized_run_hash = str(run_manifest_hash).removeprefix("sha256:")
        normalized_plan_hash = str(plan_hash).removeprefix("sha256:")
        durable_bindings = {
            "treatment_manifest_hash": str(treatment_manifest_hash).removeprefix(
                "sha256:"
            ),
            "run_input_hashes_hash": str(run_input_hashes_hash).removeprefix("sha256:"),
            "cartographer_receipt_hash": str(cartographer_receipt_hash).removeprefix(
                "sha256:"
            ),
            "cartographer_export_hash": str(cartographer_export_hash).removeprefix(
                "sha256:"
            ),
            "observation_window_hash": str(observation_window_hash).removeprefix(
                "sha256:"
            ),
        }
        if any(len(value) != 64 for value in durable_bindings.values()):
            raise ValueError("observation slot bindings must be sha256 identities")
        expected = tuple(sorted(set(expected_arm_ids)))
        if scenario.arm_id.value not in expected:
            raise ValueError("scenario arm is not authorized for this plan candidate")
        if not expected:
            raise ValueError("observation slot requires at least one expected arm")
        slot_id = canonical_observation_slot_id(
            run_manifest_hash=normalized_run_hash,
            ordinal=observation_slot_ordinal,
        )
        now = timestamp_json(datetime.now(UTC))
        with self._connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                """
                SELECT slot_ordinal, paired FROM chart_scenario_observation_slots
                WHERE campaign_id=? AND run_id=? AND candidate_id=?
                ORDER BY slot_ordinal DESC LIMIT 1
                """,
                (scenario.campaign_id, scenario.run_id, scenario.candidate_id),
            ).fetchone()
            if latest is None and observation_slot_ordinal != 1:
                raise IdempotencyConflict("first observation slot must have ordinal 1")
            if latest is not None:
                latest_ordinal = int(latest["slot_ordinal"])
                if observation_slot_ordinal > latest_ordinal + 1:
                    raise IdempotencyConflict("observation slot ordinal skipped ahead")
                if observation_slot_ordinal > latest_ordinal and not bool(
                    latest["paired"]
                ):
                    raise IdempotencyConflict(
                        "cannot advance observation slot before every expected arm is paired"
                    )
            row = conn.execute(
                """
                SELECT * FROM chart_scenario_observation_slots
                WHERE campaign_id=? AND run_id=? AND candidate_id=? AND slot_ordinal=?
                """,
                (
                    scenario.campaign_id,
                    scenario.run_id,
                    scenario.candidate_id,
                    observation_slot_ordinal,
                ),
            ).fetchone()
            if row is not None:
                if row["plan_hash"] != normalized_plan_hash:
                    raise IdempotencyConflict(
                        "observation slot was reused with a different shadow plan hash"
                    )
                if row["run_manifest_hash"] != normalized_run_hash:
                    raise IdempotencyConflict(
                        "observation slot was reused with a different run manifest hash"
                    )
                if (
                    row["slot_id"] != slot_id
                    or row["evaluated_at"] != evaluated_at
                    or row["facts_hash"] != facts_hash
                    or any(row[key] != value for key, value in durable_bindings.items())
                    or tuple(json.loads(row["expected_arm_ids_json"])) != expected
                ):
                    raise IdempotencyConflict(
                        "shared candidate observation was reused with different market facts"
                    )
                observed = set(json.loads(row["observed_arm_ids_json"]))
                created_at = str(row["created_at"])
            else:
                observed = set()
                created_at = now
            observed.add(scenario.arm_id.value)
            paired = observed == set(expected)
            proof_material = {
                "schema": OBSERVATION_SLOT_SCHEMA,
                "campaign_id": scenario.campaign_id,
                "run_id": scenario.run_id,
                "candidate_id": scenario.candidate_id,
                "slot_ordinal": observation_slot_ordinal,
                "slot_id": slot_id,
                "plan_hash": normalized_plan_hash,
                "run_manifest_hash": normalized_run_hash,
                **durable_bindings,
                "facts_hash": facts_hash,
                "evaluated_at": evaluated_at,
                "observed_arm_ids": sorted(observed),
                "expected_arm_ids": list(expected),
                "paired": paired,
            }
            proof_hash = canonical_sha256(proof_material) if paired else None
            conn.execute(
                """
                INSERT INTO chart_scenario_observation_slots
                (campaign_id, run_id, candidate_id, slot_ordinal, slot_id,
                 plan_hash, run_manifest_hash, treatment_manifest_hash,
                 run_input_hashes_hash, cartographer_receipt_hash,
                 cartographer_export_hash, observation_window_hash,
                 facts_hash, evaluated_at,
                 observed_arm_ids_json, expected_arm_ids_json, paired, proof_hash,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id, run_id, candidate_id, slot_ordinal)
                DO UPDATE SET observed_arm_ids_json=excluded.observed_arm_ids_json,
                              paired=excluded.paired,
                              proof_hash=excluded.proof_hash,
                              updated_at=excluded.updated_at
                """,
                (
                    scenario.campaign_id,
                    scenario.run_id,
                    scenario.candidate_id,
                    observation_slot_ordinal,
                    slot_id,
                    normalized_plan_hash,
                    normalized_run_hash,
                    durable_bindings["treatment_manifest_hash"],
                    durable_bindings["run_input_hashes_hash"],
                    durable_bindings["cartographer_receipt_hash"],
                    durable_bindings["cartographer_export_hash"],
                    durable_bindings["observation_window_hash"],
                    facts_hash,
                    evaluated_at,
                    json.dumps(sorted(observed), separators=(",", ":")),
                    json.dumps(list(expected), separators=(",", ":")),
                    int(paired),
                    proof_hash,
                    created_at,
                    now,
                ),
            )
        return {**proof_material, "proof_hash": proof_hash}

    def paired_market_fact_proofs(self) -> list[dict[str, Any]]:
        """Return durable paired-fact proofs for evaluation adapters."""

        if not self.db_path.exists() or not self._observation_slots_exist_readonly():
            return []
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT * FROM chart_scenario_observation_slots
                WHERE paired=1 ORDER BY run_id, candidate_id, slot_ordinal
                """
            ).fetchall()
        return [
            {
                "schema": OBSERVATION_SLOT_SCHEMA,
                "campaign_id": row["campaign_id"],
                "run_id": row["run_id"],
                "candidate_id": row["candidate_id"],
                "slot_ordinal": row["slot_ordinal"],
                "slot_id": row["slot_id"],
                "plan_hash": row["plan_hash"],
                "run_manifest_hash": row["run_manifest_hash"],
                "treatment_manifest_hash": row["treatment_manifest_hash"],
                "run_input_hashes_hash": row["run_input_hashes_hash"],
                "cartographer_receipt_hash": row["cartographer_receipt_hash"],
                "cartographer_export_hash": row["cartographer_export_hash"],
                "observation_window_hash": row["observation_window_hash"],
                "facts_hash": row["facts_hash"],
                "evaluated_at": row["evaluated_at"],
                "observed_arm_ids": json.loads(row["observed_arm_ids_json"]),
                "expected_arm_ids": json.loads(row["expected_arm_ids_json"]),
                "paired": True,
                "proof_hash": row["proof_hash"],
            }
            for row in rows
        ]

    def observation_slot_evidence(
        self,
        *,
        run_id: str,
        candidate_id: str,
        scenario_ids: tuple[str, ...],
        slot_ordinal: int,
        run_manifest_hash: str,
    ) -> dict[str, Any]:
        """Return every durable event bound to one exact candidate/slot identity."""

        slot_id = canonical_observation_slot_id(
            run_manifest_hash=run_manifest_hash, ordinal=slot_ordinal
        )
        self.ensure_schema()
        with self._connect(write=False) as conn:
            row = conn.execute(
                """
                SELECT campaign_id, run_id, candidate_id, slot_ordinal, slot_id,
                       facts_hash, paired, proof_hash
                FROM chart_scenario_observation_slots
                WHERE run_id=? AND candidate_id=? AND slot_ordinal=? AND slot_id=?
                """,
                (run_id, candidate_id, slot_ordinal, slot_id),
            ).fetchone()
            event_rows = conn.execute(
                """
                SELECT sequence, event_hash, event_json FROM chart_scenario_events
                WHERE run_id=? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        if row is None:
            raise ValueError("durable observation slot identity is missing")
        expected_scenarios = set(scenario_ids)
        events = []
        for event_row in event_rows:
            event = json.loads(event_row["event_json"])
            if (
                event.get("scenario_id") in expected_scenarios
                and event.get("market_observation_id") == slot_id
            ):
                event["event_hash"] = event_row["event_hash"]
                events.append(event)
        body = {
            "schema": "bhiksha.chart-scenario-durable-slot-evidence.v1",
            "campaign_id": row["campaign_id"],
            "run_id": row["run_id"],
            "candidate_id": row["candidate_id"],
            "slot_ordinal": row["slot_ordinal"],
            "slot_id": row["slot_id"],
            "facts_hash": row["facts_hash"],
            "paired": bool(row["paired"]),
            "paired_proof_hash": row["proof_hash"],
            "scenario_ids": sorted(expected_scenarios),
            "event_count": len(events),
            "event_hashes": [event["event_hash"] for event in events],
            "events": events,
        }
        return {**body, "content_hash": canonical_sha256(body)}

    def next_observation_slot_ordinal(
        self, *, run_id: str, candidate_ids: tuple[str, ...]
    ) -> int:
        """Return the next run-wide slot only when every candidate is aligned."""

        expected = tuple(sorted(set(candidate_ids)))
        if not expected:
            raise ValueError("at least one candidate is required")
        if not self.db_path.exists() or not self._observation_slots_exist_readonly():
            return 1
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT candidate_id, MAX(slot_ordinal) AS slot_ordinal,
                       MIN(paired) AS paired
                FROM chart_scenario_observation_slots
                WHERE run_id=?
                GROUP BY candidate_id
                """,
                (run_id,),
            ).fetchall()
        by_candidate = {str(row["candidate_id"]): row for row in rows}
        unknown = set(by_candidate) - set(expected)
        if unknown:
            raise IdempotencyConflict(
                "run contains observation slots for unknown candidates: "
                + ", ".join(sorted(unknown))
            )
        ordinals: set[int] = set()
        for candidate_id in expected:
            row = by_candidate.get(candidate_id)
            if row is None:
                ordinals.add(0)
                continue
            if not bool(row["paired"]):
                raise IdempotencyConflict(
                    f"candidate {candidate_id} has an unpaired latest slot"
                )
            ordinals.add(int(row["slot_ordinal"]))
        if len(ordinals) != 1:
            raise IdempotencyConflict("candidate observation slots are not run-aligned")
        return ordinals.pop() + 1

    def latest_observation_slot_ordinal(
        self, *, run_id: str, candidate_ids: tuple[str, ...]
    ) -> int:
        """Return the latest durable slot for crash-recovery coordination."""

        expected = set(candidate_ids)
        if (
            not expected
            or not self.db_path.exists()
            or not self._observation_slots_exist_readonly()
        ):
            return 0
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT candidate_id, MAX(slot_ordinal) AS slot_ordinal
                FROM chart_scenario_observation_slots
                WHERE run_id=?
                GROUP BY candidate_id
                """,
                (run_id,),
            ).fetchall()
        by_candidate = {
            str(row["candidate_id"]): int(row["slot_ordinal"]) for row in rows
        }
        unknown = set(by_candidate) - expected
        if unknown:
            raise IdempotencyConflict(
                "run contains observation slots for unknown candidates: "
                + ", ".join(sorted(unknown))
            )
        return max(by_candidate.values(), default=0)

    def register_scenario(
        self,
        scenario: ChartScenarioSpec,
        trigger_version: str,
        *,
        plan_hash: str,
        policy_registry_hash: str,
        treatment_hash: str,
    ) -> bool:
        """Register a scenario identity; return ``True`` only on first insert."""

        self.ensure_schema()
        campaign_id, run_id, arm_id, scenario_id, version = scenario_identity_key(
            scenario, trigger_version
        )
        now = timestamp_json(datetime.now(UTC))
        scenario_json = json.dumps(
            scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        with self._connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT scenario_hash, plan_hash, policy_registry_hash, treatment_hash
                FROM chart_scenario_states
                WHERE campaign_id=? AND run_id=? AND arm_id=? AND scenario_id=? AND trigger_version=?
                """,
                (campaign_id, run_id, arm_id, scenario_id, version),
            ).fetchone()
            if row is not None:
                if row["scenario_hash"] != scenario.scenario_hash:
                    raise ValueError(
                        "scenario identity was reused with a different scenario hash"
                    )
                if row["plan_hash"] != plan_hash:
                    raise ValueError(
                        "scenario identity was reused with a different shadow plan hash"
                    )
                if row["policy_registry_hash"] != policy_registry_hash:
                    raise ValueError(
                        "scenario identity was reused with a different policy registry hash"
                    )
                if row["treatment_hash"] != treatment_hash:
                    raise ValueError(
                        "scenario identity was reused with different executable treatment"
                    )
                return False
            conn.execute(
                """
                INSERT INTO chart_scenario_states
                (campaign_id, run_id, arm_id, scenario_id, trigger_version, scenario_hash,
                 plan_hash, policy_registry_hash, treatment_hash, status, terminal, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', 0, ?, ?)
                """,
                (
                    campaign_id,
                    run_id,
                    arm_id,
                    scenario_id,
                    version,
                    scenario.scenario_hash,
                    plan_hash,
                    policy_registry_hash,
                    treatment_hash,
                    json.dumps(
                        {
                            "scenario": json.loads(scenario_json),
                            "plan_hash": plan_hash,
                            "policy_registry_hash": policy_registry_hash,
                            "treatment_hash": treatment_hash,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
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
            kind = (
                event_type
                if isinstance(event_type, ShadowEventType)
                else ShadowEventType(event_type)
            )
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
                    raise IdempotencyConflict(
                        "event identity was replayed with different facts"
                    )
                event = ScenarioShadowEvent.model_validate(
                    json.loads(existing["event_json"])
                )
                return EventWrite(
                    event=event, created=False, idempotency_key=idempotency_key
                )

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
                raise TerminalScenarioError(
                    "terminal scenario cannot accept a new event"
                )

            previous_row = conn.execute(
                "SELECT event_hash FROM chart_scenario_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            preceding_hash = (
                previous_row["event_hash"] if previous_row is not None else None
            )
            event_id = (
                "event-"
                + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:48]
            )
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
            event_json = json.dumps(
                event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
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
            terminal_value = (
                bool(state_row["terminal"])
                or terminal
                or bool(updates.get("terminal", False))
            )
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
            rows = conn.execute(
                "SELECT event_json FROM chart_scenario_events ORDER BY sequence"
            ).fetchall()
        return [
            ScenarioShadowEvent.model_validate(json.loads(row["event_json"]))
            for row in rows
        ]

    def event_count(self) -> int:
        if not self.db_path.exists() or not self._tables_exist_readonly():
            return 0
        with self._connect(write=False) as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM chart_scenario_events").fetchone()[0]
            )

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
                event = ScenarioShadowEvent.model_validate(
                    json.loads(row["event_json"])
                )
                if event.event_hash != row["event_hash"]:
                    errors.append(
                        f"sequence {row['sequence']} stored event hash mismatch"
                    )
                if event.preceding_event_hash != previous:
                    errors.append(f"sequence {row['sequence']} predecessor mismatch")
                previous = event.event_hash
            except Exception as exc:  # noqa: BLE001 - report every corrupt ledger row
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
                "paired_market_fact_proof_count": 0,
                "broker_effect_count": 0,
                "event_chain_valid": True,
            }
        with self._connect(write=False) as conn:
            scenario_count = int(
                conn.execute("SELECT COUNT(*) FROM chart_scenario_states").fetchone()[0]
            )
            terminal_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chart_scenario_states WHERE terminal=1"
                ).fetchone()[0]
            )
            event_count = int(
                conn.execute("SELECT COUNT(*) FROM chart_scenario_events").fetchone()[0]
            )
            last = conn.execute(
                "SELECT event_hash FROM chart_scenario_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            paired_fact_count = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM chart_scenario_observation_slots WHERE paired=1"
                    ).fetchone()[0]
                )
                if self._observation_slots_exist_readonly()
                else 0
            )
        chain = self.verify_event_chain()
        return {
            "db_path": str(self.db_path),
            "scenario_count": scenario_count,
            "terminal_scenario_count": terminal_count,
            "event_count": event_count,
            "last_event_hash": last["event_hash"] if last is not None else None,
            "event_chain_valid": chain.valid,
            "event_chain_errors": list(chain.errors),
            "paired_market_fact_proof_count": paired_fact_count,
            "broker_effect_count": 0,
        }

    def _tables_exist_readonly(self) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect(write=False) as conn:
            names = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        return {"chart_scenario_states", "chart_scenario_events"}.issubset(names)

    def _observation_slots_exist_readonly(self) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect(write=False) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='chart_scenario_observation_slots'
                """
            ).fetchone()
        return row is not None


__all__ = [
    "OBSERVATION_SLOT_SCHEMA",
    "EventChainReport",
    "EventWrite",
    "IdempotencyConflict",
    "SQLiteScenarioRepository",
    "ScenarioEventRepository",
    "TerminalScenarioError",
    "canonical_observation_slot_id",
    "scenario_identity_key",
]


SQLiteScenarioRepository = ScenarioEventRepository
