"""Write the daily shadow-EV report from Bhiksha's SQLite runtime database.

Emits per-shadow-lane realized paper EV (rolling window + since-anchor totals),
win rate, avg win/loss, exit-rule mix, and an improving/degrading trend flag.
Paper marks, not broker fills.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.app.bootstrap import build_runtime
from bhiksha.ops.shadow_ev_report import (
    DEFAULT_RECENT_WINDOW,
    DEFAULT_SINCE,
    render_shadow_ev_report_telegram,
    write_shadow_ev_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Override SQLite runtime database path")
    parser.add_argument("--output-dir", default=None, help="Directory for JSON and Markdown reports")
    parser.add_argument("--active-plan", default=None, help="Optional active plan path for matching runtime config")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="Since-anchor date (YYYY-MM-DD) for the totals window")
    parser.add_argument("--recent-window", type=int, default=DEFAULT_RECENT_WINDOW, help="Rolling trade-count window")
    parser.add_argument("--telegram-summary", action="store_true", help="Print the concise phone-ready summary body")
    args = parser.parse_args(argv)

    runtime = build_runtime(active_plan_path=args.active_plan)
    db_path = Path(args.db_path or runtime.app_config.sqlite_path)
    output_dir = Path(args.output_dir or Path(runtime.app_config.playbook_artifacts_dir) / "reports")
    result = write_shadow_ev_report(
        db_path,
        output_dir=output_dir,
        since=args.since,
        recent_window=args.recent_window,
    )
    print(f"SHADOW_EV_REPORT_JSON={result.json_path}")
    print(f"SHADOW_EV_REPORT_MARKDOWN={result.markdown_path}")
    book_since = result.report["book"]["since"]
    print(f"SHADOW_EV_REPORT_SINCE_PNL={book_since['total_pnl_usd']}")
    print(f"SHADOW_EV_REPORT_SINCE_TRADES={book_since['trades']}")
    if args.telegram_summary:
        print("SHADOW_EV_REPORT_TELEGRAM_SUMMARY_BEGIN")
        print(render_shadow_ev_report_telegram(result.report))
        print("SHADOW_EV_REPORT_TELEGRAM_SUMMARY_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
