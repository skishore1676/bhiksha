"""Operator-facing session summary command."""

from __future__ import annotations

import argparse

from bhiksha.app.bootstrap import build_runtime
from bhiksha.ops.summary import build_session_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a summary of Bhiksha session events")
    parser.add_argument("--db-path", default=None, help="Override SQLite event database path")
    parser.add_argument("--recent", type=int, default=10, help="How many recent events to print")
    args = parser.parse_args(argv)

    runtime = build_runtime()
    db_path = args.db_path or runtime.app_config.sqlite_path
    summary = build_session_summary(db_path, recent_limit=args.recent)

    print(f"DB_PATH={db_path}")
    print(f"TOTAL_EVENTS={summary.total_events}")
    print("EVENT_COUNTS=" + ",".join(f"{key}:{value}" for key, value in sorted(summary.event_type_counts.items())))
    print("DEPLOYMENT_COUNTS=" + ",".join(f"{key}:{value}" for key, value in sorted(summary.deployment_event_counts.items())))
    print("LIFECYCLE_LAST=" + ",".join(f"{key}:{value}" for key, value in sorted(summary.lifecycle_last_state.items())))
    print("SIGNAL_TRUE_COUNTS=" + ",".join(f"{key}:{value}" for key, value in sorted(summary.signal_true_counts.items())))
    print("EXIT_TRUE_COUNTS=" + ",".join(f"{key}:{value}" for key, value in sorted(summary.exit_true_counts.items())))
    for event in summary.recent_events:
        print(
            "RECENT="
            f"{event.created_at}|{event.event_type}|{event.deployment_id or ''}|{event.symbol or ''}|{event.detail or ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
