"""Operator CLI for the broker-inert chart-scenario lane."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from .cycle import run_observation_cycle
from .observer import BrokerInertScenarioObserver
from .paths import require_experiment_path
from .quotes import PersistedOptionSnapshotSource
from .repository import ScenarioEventRepository
from .validation import (
    DEFAULT_SHADOW_DB_PATH,
    DEFAULT_SHADOW_PLAN_PATH,
    DEFAULT_SHADOW_RECEIPT_PATH,
    BundleValidationError,
    install_shadow_plan,
    load_bundle,
    validate_bundle,
)


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _scenario(plan: Any, scenario_id: str | None) -> Any:
    if scenario_id is None:
        if len(plan.scenarios) != 1:
            raise ValueError(
                "--scenario-id is required when the plan contains multiple scenarios"
            )
        return plan.scenarios[0]
    for item in plan.scenarios:
        if item.scenario_id == scenario_id:
            return item
    raise ValueError(f"unknown scenario_id: {scenario_id}")


def _fixture_inputs(
    args: argparse.Namespace,
) -> tuple[Any, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]], str | None]:
    fixture: Mapping[str, Any] = {}
    if args.fixture:
        raw = _read_json(args.fixture)
        if not isinstance(raw, Mapping):
            raise ValueError("fixture must be an object")
        fixture = raw
        plan = validate_bundle(fixture.get("plan", fixture.get("shadow_plan")))
    else:
        plan = load_bundle(args.plan)
    bars_raw = fixture.get("bars", []) if fixture else []
    quotes_raw = (
        fixture.get("quotes", fixture.get("option_snapshots", [])) if fixture else []
    )
    if args.bars:
        bars_raw = _read_json(args.bars)
    if args.quotes:
        quotes_raw = _read_json(args.quotes)
    if not isinstance(bars_raw, Sequence) or isinstance(
        bars_raw, (str, bytes, bytearray)
    ):
        raise TypeError("bars input must be an array")
    if not isinstance(quotes_raw, Sequence) or isinstance(
        quotes_raw, (str, bytes, bytearray)
    ):
        raise TypeError("quotes input must be an array")
    evaluated_at = args.at or fixture.get("evaluated_at")
    return plan, bars_raw, quotes_raw, evaluated_at


def _install(args: argparse.Namespace) -> int:
    receipt = install_shadow_plan(
        args.input,
        output_path=args.output,
        receipt_path=args.receipt,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def _observe_one(args: argparse.Namespace) -> int:
    plan, bars, quotes, evaluated_at = _fixture_inputs(args)
    scenario = _scenario(plan, args.scenario_id)
    repository = ScenarioEventRepository(args.db_path)
    quote_source = PersistedOptionSnapshotSource(quotes) if quotes else None
    observer = BrokerInertScenarioObserver(
        repository,
        plan=plan,
        quote_source=quote_source,
    )
    result = observer.observe_one(
        scenario,
        bars=bars,
        quote_path=quotes,
        evaluated_at=evaluated_at,
        observation_slot_ordinal=args.observation_slot,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.error is None else 2


def _status(args: argparse.Namespace) -> int:
    repository = ScenarioEventRepository(args.db_path)
    status = repository.status()
    if args.plan:
        plan = load_bundle(args.plan)
        status["plan_id"] = plan.plan_id
        status["plan_hash"] = plan.plan_hash
        status["plan_scenario_count"] = len(plan.scenarios)
        status["plan_component_manifest_hash"] = plan.component_manifest_hash
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status.get("event_chain_valid", False) else 2


def _observe_cycle(args: argparse.Namespace) -> int:
    plan = load_bundle(args.plan)
    cycle_input = _read_json(args.cycle_input)
    if not isinstance(cycle_input, Mapping):
        raise TypeError("cycle input must be an object")
    receipt = run_observation_cycle(
        plan,
        cycle_input,
        repository=ScenarioEventRepository(args.db_path),
        receipt_path=args.receipt,
        cycle_input_path=args.cycle_input,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "succeeded" else 2


def _replay_cycles(args: argparse.Namespace) -> int:
    """Rebuild receipts and the event chain from sealed inputs in a fresh namespace."""

    plan = load_bundle(args.plan)
    source = require_experiment_path(
        args.cycle_input_dir, role="replay input directory"
    )
    output = require_experiment_path(args.output, role="replay output directory")
    if output.exists() and any(output.iterdir()):
        raise ValueError("replay output directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    input_paths = sorted(source.glob("slot-*.cycle-input.json"))
    if not input_paths:
        raise ValueError("replay requires at least one sealed cycle input")
    repository = ScenarioEventRepository(output / "replay.sqlite3")
    receipts: list[dict[str, Any]] = []
    for ordinal, input_path in enumerate(input_paths, start=1):
        if input_path.name != f"slot-{ordinal:04d}.cycle-input.json":
            raise ValueError("replay cycle inputs must be exact and contiguous")
        raw = _read_json(input_path)
        if not isinstance(raw, Mapping):
            raise TypeError("replay cycle input must be an object")
        receipt_path = output / "receipts" / f"slot-{ordinal:04d}.receipt.json"
        receipts.append(
            run_observation_cycle(
                plan,
                raw,
                repository=repository,
                receipt_path=receipt_path,
                cycle_input_path=input_path,
            )
        )
    chain = repository.verify_event_chain()
    if not chain.valid:
        raise ValueError("replayed event chain is invalid")
    events = [
        {**event.model_dump(mode="json"), "event_hash": event.event_hash}
        for event in repository.events()
    ]
    events_body = {
        "schema": "bhiksha.chart-scenario-events-export.v1",
        "event_count": len(events),
        "last_event_hash": chain.last_event_hash,
        "events": events,
    }
    events_payload = {**events_body, "content_hash": canonical_sha256(events_body)}
    events_path = output / "events.json"
    events_path.write_text(
        json.dumps(events_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    body = {
        "schema": "bhiksha.chart-scenario-replay-receipt.v1",
        "plan_hash": plan.plan_hash,
        "cycle_input_hashes": [receipt["cycle_input_hash"] for receipt in receipts],
        "cycle_receipt_hashes": [receipt["receipt_hash"] for receipt in receipts],
        "events_hash": events_payload["content_hash"],
        "event_count": len(events),
        "broker_effect_count": 0,
        "effects": {"broker": False, "orders": False, "authorization": False},
    }
    replay = {**body, "receipt_hash": canonical_sha256(body)}
    (output / "replay.receipt.json").write_text(
        json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(replay, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser(
        "install", help="validate and atomically install a shadow plan"
    )
    install.add_argument(
        "--input", required=True, help="validated chart-scenario bundle JSON"
    )
    install.add_argument("--output", default=str(DEFAULT_SHADOW_PLAN_PATH))
    install.add_argument("--receipt", default=str(DEFAULT_SHADOW_RECEIPT_PATH))
    install.set_defaults(handler=_install)

    observe = subparsers.add_parser(
        "observe-one", help="run one read-only fixture observation cycle"
    )
    observe.add_argument("--plan", default=str(DEFAULT_SHADOW_PLAN_PATH))
    observe.add_argument(
        "--fixture",
        default=None,
        help="fixture object containing plan, bars, and quotes",
    )
    observe.add_argument("--bars", default=None, help="completed bars JSON array")
    observe.add_argument("--quotes", default=None, help="option snapshot JSON array")
    observe.add_argument("--scenario-id", default=None)
    observe.add_argument("--db-path", default=str(DEFAULT_SHADOW_DB_PATH))
    observe.add_argument(
        "--at", default=None, help="explicit RFC 3339 observation time"
    )
    observe.add_argument(
        "--observation-slot",
        required=True,
        type=int,
        help="positive run-owned observation cycle ordinal",
    )
    observe.set_defaults(handler=_observe_one)

    cycle = subparsers.add_parser(
        "observe-cycle",
        help="observe every installed scenario from one canonical read-only snapshot",
    )
    cycle.add_argument("--plan", default=str(DEFAULT_SHADOW_PLAN_PATH))
    cycle.add_argument("--cycle-input", required=True)
    cycle.add_argument("--db-path", default=str(DEFAULT_SHADOW_DB_PATH))
    cycle.add_argument("--receipt", required=True)
    cycle.set_defaults(handler=_observe_cycle)

    replay = subparsers.add_parser(
        "replay-cycles",
        help="purely rebuild receipts and events from sealed cycle inputs",
    )
    replay.add_argument("--plan", default=str(DEFAULT_SHADOW_PLAN_PATH))
    replay.add_argument("--cycle-input-dir", required=True)
    replay.add_argument("--output", required=True)
    replay.set_defaults(handler=_replay_cycles)

    status = subparsers.add_parser(
        "status", help="read experiment state and verify the event chain"
    )
    status.add_argument("--db-path", default=str(DEFAULT_SHADOW_DB_PATH))
    status.add_argument(
        "--plan", default=None, help="optional installed plan to revalidate"
    )
    status.set_defaults(handler=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BundleValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "broker_effect_count": 0,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
