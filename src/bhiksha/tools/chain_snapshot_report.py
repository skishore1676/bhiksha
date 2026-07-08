"""Reproduce the option-selection rejection waterfall from captured chain snapshots.

Reads ``option_chain_snapshots`` / ``option_chain_snapshot_attempts`` (written
per selection attempt by ``ExecutionPlanner`` -- see
``bhiksha.options.chain_snapshot`` and
``bhiksha.persistence.sqlite.SQLiteChainSnapshotRepository``) and answers, for
a date range: how many contracts were eliminated by each filter, and whether
a different open-interest floor would have changed the outcome.
"""

from __future__ import annotations

import argparse
import json

from bhiksha.app.bootstrap import build_runtime
from bhiksha.ops.chain_snapshot_report import rejection_waterfall, what_if_oi_floor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Override SQLite database path")
    parser.add_argument("--active-plan", default=None, help="Optional active plan path (for the default db path)")
    parser.add_argument("--start-date", required=True, help="Range start, YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="Range end, YYYY-MM-DD (inclusive)")
    parser.add_argument("--deployment-id", default=None, help="Restrict to one deployment")
    parser.add_argument("--symbol", default=None, help="Restrict to one underlying symbol")
    parser.add_argument(
        "--oi-floor",
        type=int,
        default=None,
        help="If set (requires --symbol), also report the what-if for this open-interest floor",
    )
    args = parser.parse_args(argv)

    if args.db_path:
        db_path = args.db_path
    else:
        runtime = build_runtime(active_plan_path=args.active_plan)
        db_path = runtime.app_config.sqlite_path

    waterfall = rejection_waterfall(
        db_path,
        start_date=args.start_date,
        end_date=args.end_date,
        deployment_id=args.deployment_id,
        symbol=args.symbol,
    )
    print("CHAIN_SNAPSHOT_WATERFALL")
    print(json.dumps(waterfall.as_dict(), indent=2, sort_keys=True))

    if args.oi_floor is not None:
        if not args.symbol:
            parser.error("--oi-floor requires --symbol")
        what_if = what_if_oi_floor(
            db_path,
            start_date=args.start_date,
            end_date=args.end_date,
            symbol=args.symbol,
            oi_floor=args.oi_floor,
            deployment_id=args.deployment_id,
        )
        print("CHAIN_SNAPSHOT_OI_FLOOR_WHAT_IF")
        print(
            json.dumps(
                {
                    "symbol": what_if.symbol,
                    "oi_floor": what_if.oi_floor,
                    "window_rows_considered": what_if.window_rows_considered,
                    "window_rows_would_accept": what_if.window_rows_would_accept,
                    "fallback_rows_considered": what_if.fallback_rows_considered,
                    "fallback_rows_would_accept": what_if.fallback_rows_would_accept,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
