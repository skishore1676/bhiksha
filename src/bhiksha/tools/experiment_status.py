"""Emit the read-only TradeLab experiment-status envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bhiksha.config.loader import load_active_plan
from bhiksha.ops.experiment_status import (
    build_app_experiment_status,
    collect_read_only_facts,
)


def _load_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit Sheet-derived, read-only Bhiksha experiment status"
    )
    parser.add_argument(
        "--active-plan",
        required=True,
        type=Path,
        help="Compiled active_plan.json; this command never compiles or changes it",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Existing Bhiksha SQLite DB to read in read-only mode",
    )
    parser.add_argument(
        "--observation-report",
        action="append",
        type=Path,
        default=[],
        help="Existing app-owned observation report JSON (repeatable)",
    )
    parser.add_argument(
        "--weekly-scorecard",
        action="append",
        type=Path,
        default=[],
        help="Existing weekly scorecard JSON (repeatable)",
    )
    parser.add_argument(
        "--weekly-decisions",
        action="append",
        type=Path,
        default=[],
        help="Existing weekly decision JSON (repeatable)",
    )
    parser.add_argument("--as-of", help="Stable ISO timestamp for the envelope")
    parser.add_argument("--format", choices=("json",), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    active_plan = load_active_plan(args.active_plan)
    observation_reports = [_load_json(path) for path in args.observation_report]
    scorecards = [_load_json(path) for path in args.weekly_scorecard]
    weekly_decisions = [_load_json(path) for path in args.weekly_decisions]
    facts, source_status = collect_read_only_facts(
        args.db_path,
        observation_reports=observation_reports,
        scorecards=scorecards,
        weekly_decisions=weekly_decisions,
    )
    envelope = build_app_experiment_status(
        active_plan,
        facts_by_deployment=facts,
        source_status=source_status,
        as_of=args.as_of,
    )
    print(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
