"""Write a source-owned, immutable Chart Workbench evidence packet from Bhiksha SQLite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.ops.chart_evidence_export import DEFAULT_OUTPUT_DIR, export_chart_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="bhiksha.db", type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    parser.add_argument("--date", help="Chicago trading date (YYYY-MM-DD); defaults to today")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--event-limit", type=int, default=25_000)
    args = parser.parse_args(argv)
    result = export_chart_evidence(
        args.db_path, output_dir=args.output_dir, trading_date=args.date,
        revision=args.revision, event_limit=args.event_limit,
    )
    print(json.dumps({"packet_id": result.packet["id"], "path": str(result.path), "reused": result.reused}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
