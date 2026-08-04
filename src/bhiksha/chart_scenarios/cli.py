"""Operator CLI for the broker-inert chart-scenario lane."""

from __future__ import annotations

import argparse
from pathlib import Path
import json
from typing import Any, Mapping, Sequence

from .observer import BrokerInertScenarioObserver
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
            raise ValueError("--scenario-id is required when the plan contains multiple scenarios")
        return plan.scenarios[0]
    for item in plan.scenarios:
        if item.scenario_id == scenario_id:
            return item
    raise ValueError(f"unknown scenario_id: {scenario_id}")


def _fixture_inputs(args: argparse.Namespace) -> tuple[Any, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]], str | None]:
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
    quotes_raw = fixture.get("quotes", fixture.get("option_snapshots", [])) if fixture else []
    if args.bars:
        bars_raw = _read_json(args.bars)
    if args.quotes:
        quotes_raw = _read_json(args.quotes)
    if not isinstance(bars_raw, Sequence) or isinstance(bars_raw, (str, bytes, bytearray)):
        raise ValueError("bars input must be an array")
    if not isinstance(quotes_raw, Sequence) or isinstance(quotes_raw, (str, bytes, bytearray)):
        raise ValueError("quotes input must be an array")
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
        quote_source=quote_source,
        exit_policy_registry=plan.exit_policy_registry,
    )
    result = observer.observe_one(
        scenario,
        bars=bars,
        quote_path=quotes,
        evaluated_at=evaluated_at,
        market_observation_id=args.observation_id,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="validate and atomically install a shadow plan")
    install.add_argument("--input", required=True, help="validated chart-scenario bundle JSON")
    install.add_argument("--output", default=str(DEFAULT_SHADOW_PLAN_PATH))
    install.add_argument("--receipt", default=str(DEFAULT_SHADOW_RECEIPT_PATH))
    install.set_defaults(handler=_install)

    observe = subparsers.add_parser("observe-one", help="run one read-only fixture observation cycle")
    observe.add_argument("--plan", default=str(DEFAULT_SHADOW_PLAN_PATH))
    observe.add_argument("--fixture", default=None, help="fixture object containing plan, bars, and quotes")
    observe.add_argument("--bars", default=None, help="completed bars JSON array")
    observe.add_argument("--quotes", default=None, help="option snapshot JSON array")
    observe.add_argument("--scenario-id", default=None)
    observe.add_argument("--db-path", default=str(DEFAULT_SHADOW_DB_PATH))
    observe.add_argument("--at", default=None, help="explicit RFC 3339 observation time")
    observe.add_argument("--observation-id", default=None)
    observe.set_defaults(handler=_observe_one)

    status = subparsers.add_parser("status", help="read experiment state and verify the event chain")
    status.add_argument("--db-path", default=str(DEFAULT_SHADOW_DB_PATH))
    status.add_argument("--plan", default=None, help="optional installed plan to revalidate")
    status.set_defaults(handler=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BundleValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "broker_effect_count": 0}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
