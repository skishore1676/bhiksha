"""Recovery-aware health for Public portfolio reconciliation."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any


RECOVERY_EVENT_TYPE = "reconciliation_recovered"
SUCCESS_METRIC = "portfolio_sync_ms"


def summarize_provider_reconciliation(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce durable events into observed history and the active condition."""

    counts: Counter[str] = Counter()
    runtime_issue_count = 0
    active: dict[str, Any] | None = None
    recoveries: list[dict[str, Any]] = []
    compact_events: list[dict[str, Any]] = []

    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        created_at = event.get("created_at")

        if _is_success_event(event_type, payload):
            if active is not None:
                recovery = {
                    "created_at": created_at,
                    "event_type": event_type,
                    "action": payload.get("action") or "portfolio_sync_succeeded",
                    "attempt_count": payload.get("attempt_count") or active.get("consecutive_failures"),
                    "duration_seconds": payload.get("duration_seconds"),
                    "attention_was_required": bool(
                        payload.get("attention_was_required") or active.get("attention_required")
                    ),
                }
                recoveries.append(recovery)
                compact_events.append({**recovery, "severity": "recovered"})
                active = None
            continue

        if not _is_failure_event(event_type, payload):
            continue

        severity = str(payload.get("severity") or "runtime_issue")
        if event_type == "runtime_issue":
            runtime_issue_count += 1
        else:
            counts[severity] += 1
        active = {
            "created_at": created_at,
            "event_type": event_type,
            "severity": severity,
            "reason": payload.get("reason"),
            "error": payload.get("error"),
            "consecutive_failures": payload.get("consecutive_failures"),
            "first_failure_at": payload.get("first_failure_at"),
            "failure_age_seconds": payload.get("failure_age_seconds"),
            "recovery_state": payload.get("recovery_state"),
            "attention_required": bool(payload.get("attention_required") or severity == "blocking"),
        }
        compact_events.append(active)

    active_severity = str((active or {}).get("severity") or "")
    attention_required = bool((active or {}).get("attention_required"))
    if attention_required:
        state = "needs_human"
    elif active is not None:
        state = "self_healing"
    elif recoveries:
        state = "recovered"
    else:
        state = "healthy"

    return {
        "schema": "bhiksha.provider_reconciliation.v1",
        "available": True,
        "state": state,
        "attention_required": attention_required,
        "warning_count": counts.get("warning", 0),
        "degraded_count": counts.get("degraded", 0),
        "blocking_count": counts.get("blocking", 0),
        "runtime_issue_count": runtime_issue_count,
        "active_warning_count": 1 if active_severity == "warning" else 0,
        "active_degraded_count": 1 if active_severity == "degraded" else 0,
        "active_blocking_count": 1 if active_severity == "blocking" else 0,
        "active_runtime_issue_count": 1 if active_severity == "runtime_issue" else 0,
        "recovered_count": len(recoveries),
        "last_failure": active,
        "last_recovery": recoveries[-1] if recoveries else None,
        "events": compact_events[-10:],
    }


def inspect_provider_reconciliation(db_path: str | Path, *, limit: int = 500) -> dict[str, Any]:
    """Read recent provider-reconciliation evidence without touching broker state."""

    path = Path(db_path)
    if not path.is_file():
        return empty_provider_reconciliation(available=False, reason="db_missing")
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "events" not in tables:
                return empty_provider_reconciliation(available=False, reason="events_missing")
            rows = conn.execute(
                """
                SELECT created_at, event_type, payload
                FROM events
                WHERE event_type IN (?, ?)
                   OR (
                       event_type = ?
                       AND json_valid(payload)
                       AND json_extract(payload, '$.stage') = ?
                   )
                   OR (
                       event_type = ?
                       AND json_valid(payload)
                       AND json_extract(payload, '$.metric') = ?
                   )
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    "reconciliation_health",
                    RECOVERY_EVENT_TYPE,
                    "runtime_issue",
                    "reconciliation",
                    "runtime_metric",
                    SUCCESS_METRIC,
                    max(limit, 1),
                ),
            ).fetchall()
    except sqlite3.Error:
        return empty_provider_reconciliation(available=False, reason="db_read_failed")
    events = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        events.append(
            {
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    summary = summarize_provider_reconciliation(events)
    summary["observed_at"] = datetime.now(UTC).isoformat()
    return summary


def empty_provider_reconciliation(*, available: bool = True, reason: str | None = None) -> dict[str, Any]:
    summary = summarize_provider_reconciliation([])
    summary["available"] = available
    if reason:
        summary["reason"] = reason
    return summary


def _is_failure_event(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type == "reconciliation_health":
        return True
    return event_type == "runtime_issue" and payload.get("stage") == "reconciliation"


def _is_success_event(event_type: str, payload: dict[str, Any]) -> bool:
    return event_type == RECOVERY_EVENT_TYPE or (
        event_type == "runtime_metric" and payload.get("metric") == SUCCESS_METRIC
    )
