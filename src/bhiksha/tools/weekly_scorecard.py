"""Write the weekly profile-vs-legacy scorecard from SQLite (workplan #5).

Mirrors ``bhiksha.tools.daily_report``'s invocation style. The scheduled Friday
job runs it with no overrides (deployments come from ``build_runtime``); offline
verification can point ``--db-path`` at a snapshot and ``--active-plan-json`` at
a copied active_plan.json so the evidence-gate flags are populated without a
full runtime bootstrap.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.ops.weekly_scorecard import (
    EXPERIMENT_START,
    render_weekly_scorecard_telegram_summary,
    write_weekly_scorecard,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Override SQLite database path")
    parser.add_argument("--week-start", default=None, help="Week start YYYY-MM-DD (default: Monday of week-end)")
    parser.add_argument("--week-end", default=None, help="Week end YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--experiment-start", default=EXPERIMENT_START, help="Live experiment start for the cumulative line")
    parser.add_argument("--output-dir", default=None, help="Directory for JSON and Markdown reports")
    parser.add_argument("--active-plan", default=None, help="Active plan path for build_runtime (scheduled job path)")
    parser.add_argument(
        "--active-plan-json",
        default=None,
        help="Load deployments directly from this active_plan.json (offline path; skips build_runtime)",
    )
    parser.add_argument("--telegram-summary", action="store_true", help="Print the concise Telegram receipt body")
    args = parser.parse_args(argv)

    deployments = None
    db_path: Path
    output_dir: Path

    if args.active_plan_json is not None:
        # Offline / snapshot path: no runtime bootstrap required.
        from bhiksha.config.loader import load_active_plan

        deployments = list(load_active_plan(args.active_plan_json).deployments)
        if args.db_path is None:
            parser.error("--db-path is required when using --active-plan-json")
        db_path = Path(args.db_path)
        output_dir = Path(args.output_dir or Path.cwd() / "weekly_scorecard_out")
    else:
        # Scheduled-job path: identical to daily_report -- deployments and the
        # canonical db/output paths come from the runtime bootstrap.
        from bhiksha.app.bootstrap import build_runtime

        runtime = build_runtime(active_plan_path=args.active_plan)
        deployments = runtime.deployments
        db_path = Path(args.db_path or runtime.app_config.sqlite_path)
        output_dir = Path(args.output_dir or Path(runtime.app_config.playbook_artifacts_dir) / "reports")

    result = write_weekly_scorecard(
        db_path,
        output_dir=output_dir,
        week_start=args.week_start,
        week_end=args.week_end,
        experiment_start=args.experiment_start,
        deployments=deployments,
    )
    print(f"WEEKLY_SCORECARD_JSON={result.json_path}")
    print(f"WEEKLY_SCORECARD_MARKDOWN={result.markdown_path}")
    print(f"WEEKLY_SCORECARD_WEEK={result.report['week_start']}..{result.report['week_end']}")
    if args.telegram_summary:
        print("WEEKLY_SCORECARD_TELEGRAM_SUMMARY_BEGIN")
        print(render_weekly_scorecard_telegram_summary(result.report, markdown_path=result.markdown_path))
        print("WEEKLY_SCORECARD_TELEGRAM_SUMMARY_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
