"""Session summary helpers for operator review."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True, frozen=True)
class RecentEvent:
    created_at: str
    event_type: str
    deployment_id: str | None = None
    symbol: str | None = None
    detail: str | None = None


@dataclass(slots=True, frozen=True)
class SessionSummary:
    total_events: int
    event_type_counts: dict[str, int] = field(default_factory=dict)
    deployment_event_counts: dict[str, int] = field(default_factory=dict)
    lifecycle_last_state: dict[str, str] = field(default_factory=dict)
    signal_true_counts: dict[str, int] = field(default_factory=dict)
    exit_true_counts: dict[str, int] = field(default_factory=dict)
    runtime_issue_counts: dict[str, int] = field(default_factory=dict)
    runtime_metric_latest: dict[str, float] = field(default_factory=dict)
    runtime_metric_average: dict[str, float] = field(default_factory=dict)
    latest_startup_created_at: str | None = None
    latest_startup_snapshot: dict = field(default_factory=dict)
    recent_events: list[RecentEvent] = field(default_factory=list)


def build_session_summary(db_path: str, *, recent_limit: int = 10) -> SessionSummary:
    path = Path(db_path)
    if not path.exists():
        return SessionSummary(total_events=0)

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT created_at, event_type, payload FROM events ORDER BY id"
        ).fetchall()

    event_counts: Counter[str] = Counter()
    deployment_counts: Counter[str] = Counter()
    lifecycle_last_state: dict[str, str] = {}
    signal_true_counts: Counter[str] = Counter()
    exit_true_counts: Counter[str] = Counter()
    runtime_issue_counts: Counter[str] = Counter()
    runtime_metric_values: defaultdict[str, list[float]] = defaultdict(list)
    runtime_metric_latest: dict[str, float] = {}
    latest_startup_created_at: str | None = None
    latest_startup_snapshot: dict = {}
    recent: list[RecentEvent] = []

    for created_at, event_type, payload_text in rows:
        event_counts[event_type] += 1
        payload = _safe_json(payload_text)
        deployment_id = _maybe_str(payload.get("deployment_id"))
        symbol = _maybe_str(payload.get("symbol"))
        if deployment_id:
            deployment_counts[deployment_id] += 1
        if event_type == "lifecycle_transition" and deployment_id:
            new_state = _maybe_str(payload.get("new_state"))
            if new_state:
                lifecycle_last_state[deployment_id] = new_state
        if event_type == "signal_decision" and deployment_id and bool(payload.get("signal")):
            signal_true_counts[deployment_id] += 1
        if event_type == "exit_decision" and deployment_id and bool(payload.get("exit")):
            exit_true_counts[deployment_id] += 1
        if event_type == "runtime_issue":
            category = _maybe_str(payload.get("category")) or "exception"
            runtime_issue_counts[category] += 1
        if event_type == "startup_config":
            latest_startup_created_at = created_at
            latest_startup_snapshot = payload
        if event_type == "runtime_metric":
            metric_name = _runtime_metric_name(payload)
            metric_value = _maybe_float(payload.get("value"))
            if metric_name is not None and metric_value is not None:
                runtime_metric_values[metric_name].append(metric_value)
                runtime_metric_latest[metric_name] = metric_value
        recent.append(
            RecentEvent(
                created_at=created_at,
                event_type=event_type,
                deployment_id=deployment_id,
                symbol=symbol,
                detail=_event_detail(event_type, payload),
            )
        )

    return SessionSummary(
        total_events=len(rows),
        event_type_counts=dict(event_counts),
        deployment_event_counts=dict(deployment_counts),
        lifecycle_last_state=lifecycle_last_state,
        signal_true_counts=dict(signal_true_counts),
        exit_true_counts=dict(exit_true_counts),
        runtime_issue_counts=dict(runtime_issue_counts),
        runtime_metric_latest=runtime_metric_latest,
        runtime_metric_average={
            key: round(sum(values) / len(values), 3) for key, values in runtime_metric_values.items() if values
        },
        latest_startup_created_at=latest_startup_created_at,
        latest_startup_snapshot=latest_startup_snapshot,
        recent_events=recent[-recent_limit:],
    )


def _safe_json(payload_text: str) -> dict:
    try:
        value = json.loads(payload_text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _maybe_str(value) -> str | None:
    return str(value) if value is not None else None


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _runtime_metric_name(payload: dict) -> str | None:
    metric = _maybe_str(payload.get("metric"))
    if not metric:
        return None
    symbol = _maybe_str(payload.get("symbol"))
    action = _maybe_str(payload.get("action"))
    parts = [metric]
    if symbol:
        parts.append(symbol)
    if action:
        parts.append(action)
    return ":".join(parts)


def _event_detail(event_type: str, payload: dict) -> str | None:
    if event_type == "lifecycle_transition":
        previous = payload.get("previous_state")
        new = payload.get("new_state")
        reason = payload.get("reason")
        return f"{previous}->{new} ({reason})"
    if event_type == "trade_plan":
        return payload.get("option_symbol")
    if event_type == "exit_plan":
        return payload.get("action")
    if event_type == "signal_decision":
        signal = payload.get("signal")
        direction = payload.get("direction")
        reason = ",".join(payload.get("reason") or [])
        return f"signal={signal} direction={direction} reasons={reason}"
    if event_type == "exit_decision":
        exit_now = payload.get("exit")
        action = payload.get("action")
        reason = ",".join(payload.get("reason") or [])
        return f"exit={exit_now} action={action} reasons={reason}"
    if event_type == "runtime_metric":
        metric = payload.get("metric")
        value = payload.get("value")
        unit = payload.get("unit")
        symbol = payload.get("symbol")
        action = payload.get("action")
        detail = f"{metric}={value}{unit or ''}"
        if symbol:
            detail += f" symbol={symbol}"
        if action:
            detail += f" action={action}"
        return detail
    return None
