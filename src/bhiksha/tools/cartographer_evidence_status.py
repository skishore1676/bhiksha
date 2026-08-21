"""Read-only composed status for Cartographer producer and projection evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.cartographer_profiles import canonical_hash
from bhiksha.experiments.cartographer_attempts import (
    ATTEMPT_EVENT,
    OUTCOME_EVENT,
    load_attempt_events,
    signal_attempt_id,
    trigger_accounting,
)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _compiled_cartographer_ids(active_plan_path: Path, *, run_id: str) -> set[str] | None:
    plan = _read(active_plan_path)
    if plan is None:
        return None
    deployments = plan.get("deployments")
    if not isinstance(deployments, list):
        return None
    result: set[str] = set()
    for deployment in deployments:
        if not isinstance(deployment, dict):
            return None
        metadata = ((deployment.get("source") or {}).get("metadata") or {})
        if metadata.get("source_owner") != "market_cartographer":
            continue
        if metadata.get("run_id") != run_id:
            return None
        signal_id = str(metadata.get("signal_id") or "")
        if not signal_id or signal_id in result:
            return None
        result.add(signal_id)
    return result


def _after_compile_deadline(trading_date: str, *, now: datetime | None) -> bool:
    chicago = ZoneInfo("America/Chicago")
    current = (now or datetime.now(chicago)).astimezone(chicago)
    deadline = datetime.combine(
        datetime.fromisoformat(trading_date).date(), time(8, 30), tzinfo=chicago
    )
    return current >= deadline


def build_status(
    *, producer_status_path: Path, projection_receipt_path: Path,
    active_plan_path: Path | None = None, now: datetime | None = None,
    events_db_path: Path | None = None,
) -> dict[str, Any]:
    producer = _read(producer_status_path)
    projection = _read(projection_receipt_path)
    producer_run_id = ((producer or {}).get("receipt") or {}).get("run_id")
    producer_batch_hash = ((producer or {}).get("receipt") or {}).get("signal_batch_hash")
    projection_hash_ok = bool(
        projection
        and projection.get("receipt_hash")
        == canonical_hash({key: value for key, value in projection.items() if key != "receipt_hash"})
    )
    producer_ok = bool(
        producer and producer.get("lifecycle") == "complete"
        and producer_run_id and producer_batch_hash
    )
    projection_ok = bool(
        projection and projection.get("status") in {"applied", "succeeded"}
        and projection_hash_ok
        and projection.get("producer_run_id") == producer_run_id
        and projection.get("signal_batch_hash") == producer_batch_hash
    )
    trading_date = str((projection or {}).get("trading_date") or "")
    accounting = None
    if events_db_path is not None:
        accounting = trigger_accounting(
            load_attempt_events(events_db_path),
            trading_date=trading_date or None,
        )
    expected_ids = {
        str(action.get("signal_id") or "")
        for action in ((projection or {}).get("actions") or [])
        if isinstance(action, dict) and action.get("action") in {"created", "preserved"}
    }
    compiled_ids = (
        _compiled_cartographer_ids(active_plan_path, run_id=str(producer_run_id))
        if active_plan_path is not None and producer_run_id else None
    )
    if accounting is not None and accounting["status"] == "attention":
        status, compile_status = "blocked", "trigger_accounting_attention"
    elif producer_ok and projection_ok and compiled_ids is not None and compiled_ids == expected_ids:
        status, compile_status = "healthy", "matched"
    elif (
        producer_ok and projection_ok and trading_date
        and not _after_compile_deadline(trading_date, now=now)
    ):
        status, compile_status = "compile_pending", "pending"
    else:
        status = "blocked"
        compile_status = "mismatch" if producer_ok and projection_ok else "unavailable"
    return {
        "schema": "bhiksha.cartographer_evidence_status.v1",
        "status": status,
        "attention_required": status == "blocked",
        "producer": producer or {"status": "missing"},
        "projection": projection or {"status": "missing"},
        "compile": {
            "status": compile_status,
            "active_plan_path": str(active_plan_path) if active_plan_path is not None else None,
            "expected_signal_ids": sorted(expected_ids),
            "compiled_signal_ids": sorted(compiled_ids) if compiled_ids is not None else None,
        },
        "trigger_accounting": accounting,
        "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
    }


def read_terminal_facts(events_db_path: Path) -> list[dict[str, Any]]:
    """Read and exact-receipt-dedupe Cartographer facts from Bhiksha's owner log."""

    path = events_db_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT payload FROM events WHERE event_type = ? ORDER BY id",
            ("cartographer_terminal_fact",),
        ).fetchall()
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (payload_text,) in rows:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            continue
        receipt_id = str(payload.get("fact_receipt_id") or "")
        if not receipt_id or receipt_id in seen:
            continue
        seen.add(receipt_id)
        facts.append(payload)
    return facts


def read_signal_lifecycle(events_db_path: Path) -> list[dict[str, Any]]:
    """Collapse Bhiksha-owned Cartographer events to one lifecycle state per signal."""

    path = events_db_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT event_type, payload FROM events "
            "WHERE event_type IN (?, ?, ?, ?, ?) ORDER BY id",
            (
                "signal_evaluation",
                "signal_decision",
                ATTEMPT_EVENT,
                OUTCOME_EVENT,
                "cartographer_terminal_fact",
            ),
        ).fetchall()
    lifecycle: dict[str, dict[str, Any]] = {}
    for event_type, payload_text in rows:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            continue
        if event_type == "cartographer_terminal_fact":
            signal_id = str((payload.get("identity") or {}).get("signal_id") or "")
            if not signal_id:
                continue
            lifecycle[signal_id] = {
                "signal_id": signal_id,
                # Terminal facts are emitted only from physical close
                # chokepoints. Excursion coverage is independent lifecycle
                # evidence and never changes a close into censoring.
                "status": "closed",
            }
            continue
        signal_id = str(payload.get("signal_id") or payload.get("deployment_id") or "")
        if not signal_id.startswith("mc-v1-"):
            continue
        record = lifecycle.setdefault(signal_id, {"signal_id": signal_id})
        if event_type == ATTEMPT_EVENT:
            record.update(
                {
                    "status": "triggered",
                    "signal_attempt_id": payload.get("signal_attempt_id"),
                    "signal_timestamp": payload.get("signal_timestamp"),
                    "run_id": payload.get("run_id"),
                    "cartographer_version": payload.get("cartographer_version"),
                    "profile_slug": payload.get("profile_slug"),
                    "reason": payload.get("reason") or [],
                }
            )
            continue
        if event_type == OUTCOME_EVENT:
            outcome = str(payload.get("outcome") or "")
            reason = str(payload.get("reason") or "")
            record.update(
                {
                    "status": {
                        "blocked": "blocked",
                        "failure": (
                            "no_contract"
                            if "SelectorEmptyError" in reason
                            else "failed"
                        ),
                        "infrastructure_censored": "infrastructure_censored",
                    }.get(outcome, "triggered"),
                    "signal_attempt_id": payload.get("signal_attempt_id"),
                    "signal_timestamp": payload.get("signal_timestamp"),
                    "run_id": payload.get("run_id"),
                    "cartographer_version": payload.get("cartographer_version"),
                    "profile_slug": payload.get("profile_slug"),
                    "attempt_outcome": outcome,
                    "reason": payload.get("reason"),
                }
            )
            continue
        reasons = {str(reason) for reason in (payload.get("reason") or [])}
        if payload.get("timestamp") is not None:
            record.setdefault("signal_timestamp", payload.get("timestamp"))
        if "chart_signal_expired" in reasons:
            record["status"] = "expired"
            if payload.get("timestamp") is not None:
                record["reason"] = list(reasons)
        elif payload.get("signal") is True:
            record.setdefault("status", "triggered")
            record.setdefault("attempt_outcome", None)
        else:
            record.setdefault("status", "eligible")
    # A true event from a pre-Phase-11 runtime has no attempt row.  Preserve an
    # exact, deterministic legacy identity and classify it as censored rather
    # than silently dropping it or calling it an expiration.
    for signal_id, record in lifecycle.items():
        if record.get("status") == "triggered" and not record.get("attempt_outcome"):
            raw_timestamp = record.get("signal_timestamp")
            try:
                timestamp = datetime.fromisoformat(
                    str(raw_timestamp or "1970-01-01T00:00:00+00:00").replace(
                        "Z", "+00:00"
                    )
                )
            except ValueError:
                timestamp = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
            record["status"] = "infrastructure_censored"
            record["attempt_outcome"] = "infrastructure_censored"
            record["signal_attempt_id"] = record.get("signal_attempt_id") or signal_attempt_id(
                deployment_id=signal_id,
                timestamp=timestamp,
                signal_id=signal_id,
            )
            record["trigger_reason"] = record.get("reason")
            record["reason"] = "triggered_without_terminal_outcome"
    return [lifecycle[signal_id] for signal_id in sorted(lifecycle)]


def build_fact_graph(*, signal_batch_path: Path, terminal_facts_path: Path) -> dict[str, Any]:
    """Read-only identity graph; absent facts remain absent rather than zero P&L."""

    batch = _read(signal_batch_path)
    facts_value = json.loads(terminal_facts_path.read_text(encoding="utf-8"))
    if not isinstance(batch, dict) or not isinstance(facts_value, list):
        raise ValueError("signal batch and terminal facts must be JSON object/array")
    facts = {
        str((fact.get("identity") or {}).get("signal_id") or ""): fact
        for fact in facts_value if isinstance(fact, dict)
    }
    nodes = []
    for signal in batch.get("signals", []):
        if not isinstance(signal, dict):
            continue
        signal_id = str(signal.get("signal_id") or "")
        fact = facts.get(signal_id)
        identity = (fact or {}).get("identity") or {}
        nodes.append({
            "signal_id": signal_id,
            "deployment_id": identity.get("deployment_id"),
            "trade_id": identity.get("trade_id"),
            "fact_receipt_id": (fact or {}).get("fact_receipt_id"),
            "lifecycle": (fact or {}).get("status", "emitted_without_terminal_fact"),
        })
    return {"schema": "bhiksha.cartographer_fact_graph.v1", "nodes": nodes,
            "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-status", type=Path)
    parser.add_argument("--projection-receipt", type=Path)
    parser.add_argument("--active-plan", type=Path)
    parser.add_argument("--events-db", type=Path)
    parser.add_argument("--signal-batch", type=Path)
    parser.add_argument("--terminal-facts", type=Path)
    args = parser.parse_args(argv)
    if args.events_db is not None and not (
        args.producer_status is not None and args.projection_receipt is not None
    ):
        if args.terminal_facts is not None:
            raise SystemExit("--events-db and --terminal-facts are mutually exclusive")
        facts = read_terminal_facts(args.events_db)
        if args.signal_batch is None:
            payload = {
                "schema": "bhiksha.cartographer_evidence_export.v1",
                "facts": facts,
                "lifecycle": read_signal_lifecycle(args.events_db),
            }
        else:
            batch = _read(args.signal_batch)
            if batch is None:
                raise SystemExit("--signal-batch must be a JSON object")
            by_signal = {
                str((fact.get("identity") or {}).get("signal_id") or ""): fact
                for fact in facts
            }
            nodes = []
            for signal in batch.get("signals", []):
                signal_id = str(signal.get("signal_id") or "")
                fact = by_signal.get(signal_id)
                identity = (fact or {}).get("identity") or {}
                nodes.append({
                    "signal_id": signal_id,
                    "deployment_id": identity.get("deployment_id"),
                    "trade_id": identity.get("trade_id"),
                    "fact_receipt_id": (fact or {}).get("fact_receipt_id"),
                    "lifecycle": (fact or {}).get("status", "emitted_without_terminal_fact"),
                })
            payload = {"schema": "bhiksha.cartographer_fact_graph.v1", "nodes": nodes}
    elif args.signal_batch is not None or args.terminal_facts is not None:
        if args.signal_batch is None or args.terminal_facts is None:
            raise SystemExit("--signal-batch and --terminal-facts must be supplied together")
        payload = build_fact_graph(
            signal_batch_path=args.signal_batch, terminal_facts_path=args.terminal_facts
        )
    else:
        if args.producer_status is None or args.projection_receipt is None:
            raise SystemExit("status mode requires --producer-status and --projection-receipt")
        payload = build_status(
            producer_status_path=args.producer_status,
            projection_receipt_path=args.projection_receipt,
            active_plan_path=args.active_plan,
            events_db_path=args.events_db,
        )
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
