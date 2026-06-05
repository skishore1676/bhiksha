"""Write a date-scoped Bhiksha trade-session report from SQLite."""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.app.bootstrap import build_runtime
from bhiksha.ops.daily_report import render_daily_report_telegram_summary, write_daily_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Override SQLite event database path")
    parser.add_argument("--trading-date", default=None, help="Report date in YYYY-MM-DD format; defaults to today UTC")
    parser.add_argument("--output-dir", default=None, help="Directory for JSON and Markdown reports")
    parser.add_argument("--active-plan", default=None, help="Optional active plan path for matching runtime config")
    parser.add_argument("--telegram-summary", action="store_true", help="Print a concise human-readable Telegram receipt body")
    args = parser.parse_args(argv)

    runtime = build_runtime(active_plan_path=args.active_plan)
    db_path = Path(args.db_path or runtime.app_config.sqlite_path)
    output_dir = Path(args.output_dir or Path(runtime.app_config.playbook_artifacts_dir) / "reports")
    result = write_daily_report(db_path, output_dir=output_dir, trading_date=args.trading_date)
    print(f"DAILY_REPORT_JSON={result.json_path}")
    print(f"DAILY_REPORT_MARKDOWN={result.markdown_path}")
    print(f"DAILY_REPORT_STATUS={result.report['status']['level']}")
    if args.telegram_summary:
        print("DAILY_REPORT_TELEGRAM_SUMMARY_BEGIN")
        print(render_daily_report_telegram_summary(result.report, markdown_path=result.markdown_path))
        print("DAILY_REPORT_TELEGRAM_SUMMARY_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
