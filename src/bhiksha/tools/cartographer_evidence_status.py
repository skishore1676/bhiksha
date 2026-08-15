"""Read-only composed status for Cartographer producer and projection evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_status(*, producer_status_path: Path, projection_receipt_path: Path) -> dict[str, Any]:
    producer = _read(producer_status_path)
    projection = _read(projection_receipt_path)
    producer_ok = bool(producer and producer.get("lifecycle") == "complete")
    projection_ok = bool(projection and projection.get("status") == "applied")
    return {
        "schema": "bhiksha.cartographer_evidence_status.v1",
        "status": "healthy" if producer_ok and projection_ok else "blocked",
        "producer": producer or {"status": "missing"},
        "projection": projection or {"status": "missing"},
        "compile": {"status": "owned_by_existing_0820_compiler"},
        "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
    }


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
    parser.add_argument("--producer-status", type=Path, required=True)
    parser.add_argument("--projection-receipt", type=Path, required=True)
    parser.add_argument("--signal-batch", type=Path)
    parser.add_argument("--terminal-facts", type=Path)
    args = parser.parse_args(argv)
    if (args.signal_batch is None) != (args.terminal_facts is None):
        raise SystemExit("--signal-batch and --terminal-facts must be supplied together")
    payload = (
        build_fact_graph(signal_batch_path=args.signal_batch, terminal_facts_path=args.terminal_facts)
        if args.signal_batch else build_status(producer_status_path=args.producer_status, projection_receipt_path=args.projection_receipt)
    )
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
