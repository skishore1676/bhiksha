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
    return None
