"""Independent supervision for entry reconciliation holds.

The live runtime owns broker reconciliation. This module only observes durable
trade/event state, records receipts, and asks for human attention when the
runtime has exhausted a bounded self-healing window.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from bhiksha.ops.alerts import AlertMode, AlertResult, send_lathi_alert


ATTENTION_AFTER_SECONDS = 300
RECONCILIATION_HOLD_STATUS = "pending_entry_reconcile"
RECONCILIATION_START_EVENT = "entry_fill_timeout_reconcile"
RECONCILIATION_RECOVERY_EVENTS = {
    "entry_reconcile_released": "released_no_fill",
    "entry_reconcile_terminal_fill_recovered": "terminal_fill_recovered",
    "entry_reconcile_recovered": "broker_position_recovered",
}


def inspect_reconciliation_state(
    db_path: str | Path,
    *,
    now: datetime | None = None,
    attention_after_seconds: int = ATTENTION_AFTER_SECONDS,
) -> dict[str, Any]:
    path = Path(db_path)
    observed_at = _as_utc(now or datetime.now(UTC))
    if not path.is_file():
        return _empty_summary(
            observed_at,
            available=False,
            reason="db_missing",
            attention_after_seconds=attention_after_seconds,
        )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trade_sessions" not in tables:
            return _empty_summary(
                observed_at,
                available=False,
                reason="trade_sessions_missing",
                attention_after_seconds=attention_after_seconds,
            )
        rows = conn.execute(
            """
            SELECT trade_id, deployment_id, symbol, option_symbol, quantity,
                   entry_order_id, entry_timestamp, status, updated_at
            FROM trade_sessions
            WHERE status = ?
            ORDER BY updated_at ASC, trade_id ASC
            """,
            (RECONCILIATION_HOLD_STATUS,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        active_trade_ids = {str(row["trade_id"]) for row in rows}
        if active_trade_ids and "events" in tables:
            event_types = (RECONCILIATION_START_EVENT, *sorted(RECONCILIATION_RECOVERY_EVENTS))
            placeholders = ",".join("?" for _ in event_types)
            event_rows = conn.execute(
                f"""
                SELECT created_at, event_type, payload
                FROM events
                WHERE event_type IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                event_types,
            ).fetchall()
            for event_row in event_rows:
                try:
                    payload = json.loads(event_row["payload"])
                except (TypeError, ValueError):
                    continue
                if str(payload.get("trade_id") or "") not in active_trade_ids:
                    continue
                events.append(
                    {
                        "created_at": event_row["created_at"],
                        "event_type": event_row["event_type"],
                        "payload": payload,
                    }
                )

    return summarize_reconciliation_state(
        [dict(row) for row in rows],
        events=events,
        now=observed_at,
        attention_after_seconds=attention_after_seconds,
    )


def summarize_reconciliation_state(
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    attention_after_seconds: int = ATTENTION_AFTER_SECONDS,
) -> dict[str, Any]:
    observed_at = _as_utc(now or datetime.now(UTC))
    starts: dict[str, datetime] = {}
    recoveries: dict[str, dict[str, Any]] = {}

    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        trade_id = str(payload.get("trade_id") or "")
        if not trade_id:
            continue
        event_at = _parse_timestamp(event.get("created_at"))
        event_type = str(event.get("event_type") or "")
        if event_type == RECONCILIATION_START_EVENT and event_at is not None:
            starts.setdefault(trade_id, event_at)
        action = RECONCILIATION_RECOVERY_EVENTS.get(event_type)
        if action is None or event_at is None:
            continue
        existing = recoveries.get(trade_id)
        # The terminal-fill event is the strongest account of what happened;
        # the generic tracker-recovered event may follow in the same sweep.
        if existing is not None and existing["action"] == "terminal_fill_recovered":
            continue
        started_at = starts.get(trade_id)
        recoveries[trade_id] = {
            "trade_id": trade_id,
            "deployment_id": payload.get("deployment_id"),
            "symbol": payload.get("symbol"),
            "entry_order_id": payload.get("entry_order_id"),
            "action": action,
            "recovered_at": event_at.isoformat(),
            "duration_seconds": _duration_seconds(started_at, event_at),
            "human_action_required": False,
        }

    active_holds: list[dict[str, Any]] = []
    for trade in trades:
        if str(trade.get("status") or "").lower() != RECONCILIATION_HOLD_STATUS:
            continue
        trade_id = str(trade.get("trade_id") or "")
        started_at = starts.get(trade_id) or _parse_timestamp(trade.get("updated_at")) or _parse_timestamp(
            trade.get("entry_timestamp")
        )
        age_seconds = _duration_seconds(started_at, observed_at)
        needs_human = age_seconds is None or age_seconds >= max(attention_after_seconds, 0)
        active_holds.append(
            {
                "trade_id": trade_id,
                "deployment_id": trade.get("deployment_id"),
                "symbol": trade.get("symbol"),
                "option_symbol": trade.get("option_symbol"),
                "quantity": trade.get("quantity"),
                "entry_order_id": trade.get("entry_order_id"),
                "started_at": started_at.isoformat() if started_at is not None else None,
                "age_seconds": age_seconds,
                "state": "needs_human" if needs_human else "self_healing",
                "human_action_required": needs_human,
                "blocked_scope": "deployment",
            }
        )

    needs_human_holds = [hold for hold in active_holds if hold["human_action_required"]]
    self_healing_holds = [hold for hold in active_holds if not hold["human_action_required"]]
    state = "needs_human" if needs_human_holds else "self_healing" if self_healing_holds else "healthy"
    return {
        "schema": "bhiksha.entry_reconciliation.v1",
        "observed_at": observed_at.isoformat(),
        "available": True,
        "state": state,
        "attention_after_seconds": attention_after_seconds,
        "attention_required": bool(needs_human_holds),
        "active_count": len(active_holds),
        "self_healing_count": len(self_healing_holds),
        "needs_human_count": len(needs_human_holds),
        "recovered_count": len(recoveries),
        "active_holds": active_holds,
        "recoveries": sorted(recoveries.values(), key=lambda item: (item["recovered_at"], item["trade_id"])),
        "released_no_fill_trade_ids": sorted(
            trade_id for trade_id, recovery in recoveries.items() if recovery["action"] == "released_no_fill"
        ),
    }


def run_reconciliation_supervisor(
    db_path: str | Path,
    *,
    receipt_dir: str | Path,
    alert_mode: AlertMode = "live",
    alert_profile: str | None = None,
    now: datetime | None = None,
    attention_after_seconds: int = ATTENTION_AFTER_SECONDS,
    alert_sender: Callable[..., AlertResult] = send_lathi_alert,
) -> dict[str, Any]:
    observed_at = _as_utc(now or datetime.now(UTC))
    summary = inspect_reconciliation_state(
        db_path,
        now=observed_at,
        attention_after_seconds=attention_after_seconds,
    )
    target_dir = Path(receipt_dir)
    latest_path = target_dir / "latest.json"
    previous = _read_json(latest_path)
    previous_alert_open = bool(previous.get("alert_open"))
    current_attention_keys = _attention_keys(summary.get("active_holds") or [])
    previous_alerted_keys = {
        str(key) for key in (previous.get("alerted_attention_keys") or []) if str(key)
    }
    # Backward compatibility for the first run after this receipt schema: an
    # open alert without per-order keys already covered the then-active set.
    if previous_alert_open and "alerted_attention_keys" not in previous and not previous_alerted_keys:
        previous_alerted_keys = _attention_keys(previous.get("active_holds") or [])
    alerted_attention_keys = previous_alerted_keys & current_attention_keys
    new_attention_keys = current_attention_keys - alerted_attention_keys
    fingerprint = _attention_fingerprint(current_attention_keys)

    alert: AlertResult | None = None
    alert_reason = "not_required"
    alert_open = previous_alert_open
    if summary["attention_required"]:
        if new_attention_keys:
            alert = alert_sender(
                title="Bhiksha entry reconciliation needs help",
                body=_attention_body(summary),
                level="error",
                mode=alert_mode,
                profile=alert_profile,
                template="urgent_gate",
                link_preview="disabled",
            )
            alert_reason = "new_attention_state"
            if alert.ok:
                alerted_attention_keys = set(current_attention_keys)
        else:
            alert_reason = "duplicate_suppressed"
        alert_open = previous_alert_open or bool(alerted_attention_keys)
    elif previous_alert_open:
        alert = alert_sender(
            title="Bhiksha entry reconciliation recovered",
            body="The previously escalated entry reconciliation hold is clear. Affected lanes are no longer blocked by that hold.",
            level="info",
            mode=alert_mode,
            profile=alert_profile,
            template="status",
            link_preview="disabled",
        )
        alert_reason = "attention_cleared"
        if alert.ok or alert_mode == "off":
            alert_open = False
            alerted_attention_keys = set()

    receipt = {
        **summary,
        "job_status": "attention_required" if summary["attention_required"] else "ok",
        "alert_open": alert_open,
        "attention_fingerprint": fingerprint,
        "alerted_attention_keys": sorted(alerted_attention_keys),
        "alert_reason": alert_reason,
        "alert": alert.to_dict() if alert is not None else None,
    }
    try:
        _write_receipt(target_dir, receipt)
    except OSError as exc:
        # Receipt persistence is observational. Do not turn a delivered safety
        # alert into an immediate second "launchd job failed" interruption.
        receipt["receipt_error"] = str(exc)
    return receipt


def _attention_body(summary: dict[str, Any]) -> str:
    lines = [
        "Bhiksha exhausted its automatic entry-reconciliation window.",
        "Affected deployments remain fail-closed; no duplicate entry will be submitted.",
        "",
    ]
    for hold in (summary.get("active_holds") or []):
        if not hold.get("human_action_required"):
            continue
        age = hold.get("age_seconds")
        age_text = f"{int(age // 60)}m {int(age % 60)}s" if isinstance(age, (int, float)) else "unknown"
        lines.append(
            f"- {hold.get('symbol')} / {hold.get('deployment_id')}: order {hold.get('entry_order_id')}, "
            f"hold age {age_text}; deployment blocked"
        )
    lines.extend(["", "Required: verify the listed order state in Public before manually releasing or retrying the lane."])
    return "\n".join(lines)


def _attention_keys(holds: list[dict[str, Any]]) -> set[str]:
    return {
        f"{hold.get('trade_id')}:{hold.get('entry_order_id')}"
        for hold in holds
        if hold.get("human_action_required")
    }


def _attention_fingerprint(identities: set[str]) -> str:
    if not identities:
        return ""
    return hashlib.sha256("|".join(sorted(identities)).encode("utf-8")).hexdigest()[:20]


def _write_receipt(target_dir: Path, receipt: dict[str, Any]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    latest = target_dir / "latest.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(latest)
    with (target_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True, default=str) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _empty_summary(
    observed_at: datetime,
    *,
    available: bool,
    reason: str,
    attention_after_seconds: int,
) -> dict[str, Any]:
    return {
        "schema": "bhiksha.entry_reconciliation.v1",
        "observed_at": observed_at.isoformat(),
        "available": available,
        "reason": reason,
        "state": "no_data",
        "attention_after_seconds": attention_after_seconds,
        "attention_required": False,
        "active_count": 0,
        "self_healing_count": 0,
        "needs_human_count": 0,
        "recovered_count": 0,
        "active_holds": [],
        "recoveries": [],
        "released_no_fill_trade_ids": [],
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _duration_seconds(start: datetime | None, end: datetime) -> int | None:
    if start is None:
        return None
    return max(0, int((end - start).total_seconds()))
