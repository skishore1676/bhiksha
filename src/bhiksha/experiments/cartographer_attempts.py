"""Durable identity and bounded recovery helpers for Cartographer triggers.

The Cartographer lane is shadow-only, but its trigger is still an operational
fact.  Keep the identity and terminal-outcome contract in one small, pure
module so the runtime, read-only evidence export, and tests share the same
rules without adding another persistence surface.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ATTEMPT_EVENT = "cartographer_signal_attempt"
OUTCOME_EVENT = "cartographer_signal_attempt_outcome"
RECOVERY_EVENT = "cartographer_signal_attempt_recovery"
TERMINAL_OUTCOMES = frozenset(
    {"execution", "blocked", "failure", "infrastructure_censored"}
)


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    source = getattr(value, "source", None)
    metadata = getattr(source, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def is_cartographer_deployment(value: Any) -> bool:
    """Return whether a deployment/payload belongs to the Cartographer owner."""

    return _metadata(value).get("source_owner") == "market_cartographer"


def signal_attempt_id(
    *,
    deployment_id: str,
    timestamp: datetime,
    active_plan_id: str | None = None,
    session_id: str | None = None,
    signal_id: str | None = None,
) -> str:
    """Derive a stable identity for one point-in-time true signal occurrence."""

    observed_at = timestamp.astimezone(UTC).isoformat()
    material = "|".join(
        (
            str(active_plan_id or ""),
            str(session_id or ""),
            str(signal_id or deployment_id),
            str(deployment_id),
            observed_at,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"sa-v1-{digest[:32]}"


def attempt_context(
    deployment: Any,
    *,
    deployment_id: str,
    timestamp: datetime,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the immutable identity fields shared by start/outcome events."""

    metadata = _metadata(deployment)
    if metadata.get("source_owner") != "market_cartographer":
        return None
    identity = identity or {}
    active_plan_id = identity.get("active_plan_id") or metadata.get("active_plan_id")
    session_id = identity.get("session_id") or metadata.get("session_id")
    signal_id = str(metadata.get("signal_id") or deployment_id)
    return {
        "signal_attempt_id": signal_attempt_id(
            deployment_id=deployment_id,
            timestamp=timestamp,
            active_plan_id=str(active_plan_id or "") or None,
            session_id=str(session_id or "") or None,
            signal_id=signal_id,
        ),
        "signal_id": signal_id,
        "deployment_id": deployment_id,
        "symbol": str(getattr(deployment, "symbol", "") or ""),
        "signal_timestamp": timestamp.astimezone(UTC).isoformat(),
        "active_plan_id": active_plan_id,
        "session_id": session_id,
        "run_id": metadata.get("run_id"),
        "cartographer_version": metadata.get("cartographer_version"),
        "profile_slug": metadata.get("profile_slug") or metadata.get("management_policy"),
        "bundle_hash": metadata.get("bundle_hash"),
        "valid_after": metadata.get("valid_after"),
        "valid_through": metadata.get("valid_through"),
        "execution_mode": "shadow" if getattr(getattr(deployment, "execution", None), "shadow_only", False) else "live",
    }


def attempt_start_payload(
    context: Mapping[str, Any],
    *,
    decision_reason: Sequence[str] = (),
    direction: str | None = None,
    features: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **dict(context),
        "status": "triggered",
        "reason": list(decision_reason),
        "direction": direction,
        "features": dict(features or {}),
    }


def attempt_outcome_payload(
    context: Mapping[str, Any],
    *,
    outcome: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError(f"unsupported Cartographer attempt outcome: {outcome}")
    return {
        **dict(context),
        "outcome": outcome,
        "reason": reason,
        "details": dict(details or {}),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recovery_action(
    attempt: Mapping[str, Any],
    *,
    now: datetime,
    shadow_only: bool,
    has_existing_entry: bool,
) -> tuple[str, str]:
    """Classify one unresolved attempt without performing an execution."""

    if attempt.get("recovery_attempted"):
        return "censor", "recovery_already_attempted"
    if not shadow_only:
        return "censor", "live_replay_forbidden"
    if has_existing_entry:
        return "censor", "existing_entry_or_trade"
    valid_after = _parse_timestamp(attempt.get("valid_after"))
    valid_through = _parse_timestamp(attempt.get("valid_through"))
    current = now.astimezone(UTC)
    if valid_after is None or valid_through is None:
        return "censor", "freshness_contract_missing"
    if current < valid_after:
        return "defer", "observation_not_yet_valid"
    if current > valid_through:
        return "censor", "chart_entry_observation_stale"
    observed = _parse_timestamp(attempt.get("signal_timestamp"))
    if observed is None:
        return "censor", "freshness_contract_missing"
    # Keep restart recovery on the same one-minute point-in-time freshness
    # contract enforced by the normal Cartographer entry guard.  A signal can
    # still be inside its calendar validity window while its observation is no
    # longer safe to replay.
    if (current - observed).total_seconds() > 60:
        return "censor", "chart_entry_observation_stale"
    return "replay", "fresh_shadow_recovery"


def load_attempt_events(path: str | Path, *, limit: int = 2_000) -> list[dict[str, Any]]:
    """Read only the bounded Cartographer attempt slice from the event ledger."""

    database = Path(path).expanduser().resolve()
    if not database.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT id, created_at, event_type, payload FROM events "
                "WHERE event_type IN (?, ?, ?, ?, ?) ORDER BY id DESC LIMIT ?",
                (
                    ATTEMPT_EVENT,
                    OUTCOME_EVENT,
                    RECOVERY_EVENT,
                    "signal_decision",
                    "trade_plan",
                    int(limit),
                ),
            ).fetchall()
    except sqlite3.Error:
        return []
    events: list[dict[str, Any]] = []
    for event_id, created_at, event_type, payload_text in reversed(rows):
        try:
            payload = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            events.append(
                {
                    "event_id": int(event_id),
                    "created_at": created_at,
                    "event_type": event_type,
                    "payload": payload,
                }
            )
    return events


def unresolved_attempts(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return starts with no terminal outcome, preserving recovery markers."""

    starts: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trade_plans: dict[str, list[dict[str, Any]]] = defaultdict(list)
    recoveries: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        attempt_id = str(payload.get("signal_attempt_id") or "")
        if event_type in {"signal_decision", "signal_evaluation"}:
            # A new runtime writes the signal decision (with its durable ID)
            # immediately before the dedicated attempt row.  Keep the former
            # as a bounded fallback if interruption happens between appends;
            # legacy rows without an ID remain read-only evidence only.
            if payload.get("signal") is not True or not attempt_id:
                continue
            deployment_id = str(payload.get("deployment_id") or "")
            starts.setdefault(
                attempt_id,
                {
                    "signal_attempt_id": attempt_id,
                    "signal_id": str(payload.get("signal_id") or deployment_id),
                    "deployment_id": deployment_id,
                    "symbol": payload.get("symbol"),
                    "signal_timestamp": payload.get("timestamp"),
                    "reason": payload.get("reason") or [],
                    "direction": payload.get("direction"),
                    "features": payload.get("features") or {},
                },
            )
        elif event_type == ATTEMPT_EVENT and attempt_id:
            existing = starts.get(attempt_id)
            if existing is None:
                starts[attempt_id] = dict(payload)
            else:
                merged = dict(existing)
                merged.update(payload)
                starts[attempt_id] = merged
        elif event_type == OUTCOME_EVENT:
            if not attempt_id:
                continue
            outcomes[attempt_id].append(dict(payload))
        elif event_type == "trade_plan" and attempt_id:
            trade_plans[attempt_id].append(dict(payload))
        elif event_type == RECOVERY_EVENT:
            if attempt_id:
                recoveries.add(attempt_id)
    return [
        {
            **start,
            "outcome_count": len(outcomes.get(attempt_id, [])),
            "outcomes": outcomes.get(attempt_id, []),
            "trade_plans": trade_plans.get(attempt_id, []),
            "recovery_attempted": attempt_id in recoveries,
        }
        for attempt_id, start in starts.items()
        if not outcomes.get(attempt_id)
    ]


def trigger_accounting(
    events: Sequence[Mapping[str, Any]], *, trading_date: str | None = None
) -> dict[str, Any]:
    """Count true triggers against exactly one terminal outcome each.

    Operational health is daily.  When ``trading_date`` is supplied, retain
    historical events in the ledger but exclude starts outside that New York
    trading date so a prior infrastructure-censored session cannot poison
    every later day's owner status.
    """

    starts: dict[str, Mapping[str, Any]] = {}
    outcomes: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        attempt_id = str(payload.get("signal_attempt_id") or "")
        event_type = str(event.get("event_type") or "")
        if event_type in {"signal_decision", "signal_evaluation"} and payload.get("signal") is True:
            # Pre-Phase-11 ledgers contain signal rows but no durable attempt
            # event. Count them as unresolved evidence. New rows already carry
            # the durable ID; old rows receive a deterministic legacy ID.
            deployment_id = str(payload.get("deployment_id") or "")
            if not deployment_id.startswith("mc-v1-"):
                continue
            timestamp = _parse_timestamp(payload.get("timestamp")) or datetime.fromtimestamp(0, UTC)
            if (
                trading_date
                and timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                != trading_date
            ):
                continue
            attempt_id = attempt_id or signal_attempt_id(
                deployment_id=deployment_id,
                timestamp=timestamp,
                signal_id=deployment_id,
            )
            starts.setdefault(
                attempt_id,
                {
                    "signal_attempt_id": attempt_id,
                    "signal_id": str(payload.get("signal_id") or deployment_id),
                    "deployment_id": deployment_id,
                    "signal_timestamp": timestamp.isoformat(),
                    "legacy": not bool(payload.get("signal_attempt_id")),
                },
            )
        elif event_type == ATTEMPT_EVENT and attempt_id:
            timestamp = _parse_timestamp(payload.get("signal_timestamp"))
            if (
                trading_date
                and (
                    timestamp is None
                    or timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                    != trading_date
                )
            ):
                continue
            existing = starts.get(attempt_id)
            if existing is None:
                starts[attempt_id] = payload
            else:
                merged = dict(existing)
                merged.update(payload)
                starts[attempt_id] = merged
        elif event_type == OUTCOME_EVENT:
            if attempt_id:
                outcomes[attempt_id].append(payload)
    counts = {
        "execution": 0,
        "blocked": 0,
        "failure": 0,
        "infrastructure_censored": 0,
    }
    duplicate_outcome_attempts: list[str] = []
    unresolved: list[str] = []
    for attempt_id in starts:
        terminal = outcomes.get(attempt_id, [])
        if len(terminal) != 1:
            if len(terminal) == 0:
                unresolved.append(attempt_id)
            else:
                duplicate_outcome_attempts.append(attempt_id)
            continue
        outcome = str(terminal[0].get("outcome") or "")
        if outcome in counts:
            counts[outcome] += 1
        else:
            unresolved.append(attempt_id)
    true_triggers = len(starts)
    accounted = sum(counts.values())
    remainder = true_triggers - accounted
    return {
        "true_triggers": true_triggers,
        "executed_attempts": counts["execution"],
        "legitimate_blocks": counts["blocked"],
        "explicit_failures": counts["failure"],
        "infrastructure_censored": counts["infrastructure_censored"],
        "accounted": accounted,
        "remainder": remainder,
        "unresolved_attempt_ids": sorted(unresolved),
        "duplicate_outcome_attempt_ids": sorted(duplicate_outcome_attempts),
        "status": "healthy" if remainder == 0 and not duplicate_outcome_attempts else "attention",
    }


__all__ = [
    "ATTEMPT_EVENT",
    "OUTCOME_EVENT",
    "RECOVERY_EVENT",
    "TERMINAL_OUTCOMES",
    "attempt_context",
    "attempt_outcome_payload",
    "attempt_start_payload",
    "is_cartographer_deployment",
    "load_attempt_events",
    "recovery_action",
    "signal_attempt_id",
    "trigger_accounting",
    "unresolved_attempts",
]
