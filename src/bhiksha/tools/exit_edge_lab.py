"""Run the read-only paired Exit Edge Lab from a fixture or SQLite snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.ops.exit_edge_lab import (
    analyze_cases,
    build_historical_coverage_report,
    load_fixture_cases,
    write_exit_edge_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture-json", help="Pinned same-entry fixture with complete premium paths")
    source.add_argument("--db-path", help="Read-only Bhiksha SQLite snapshot")
    parser.add_argument("--start", help="SQLite window start YYYY-MM-DD")
    parser.add_argument("--end", help="SQLite window end YYYY-MM-DD")
    parser.add_argument("--output-dir", default="exit_edge_lab_out")
    args = parser.parse_args(argv)

    if args.fixture_json:
        cases = load_fixture_cases(args.fixture_json)
        report = analyze_cases(cases)
    else:
        missing = [name for name in ("start", "end") if getattr(args, name) is None]
        if missing:
            parser.error("--db-path requires --start and --end")
        report = build_historical_coverage_report(args.db_path, start=args.start, end=args.end)
    json_path, markdown_path = write_exit_edge_report(report, Path(args.output_dir))
    if report["report_type"] == "historical_pairing_coverage":
        print(f"EXIT_EDGE_LAB_ELIGIBLE={report['counts']['eligible_paired_trades']}")
        print(f"EXIT_EDGE_LAB_VERDICT={report['verdict']}")
        return 0
    summary = report["summary"]
    print(f"EXIT_EDGE_LAB_JSON={json_path}")
    print(f"EXIT_EDGE_LAB_MARKDOWN={markdown_path}")
    print(f"EXIT_EDGE_LAB_PAIRED={summary['paired_count']}")
    print(f"EXIT_EDGE_LAB_INSUFFICIENT={summary['insufficient_count']}")
    print(f"EXIT_EDGE_LAB_CONFIDENCE={summary['confidence']['indicator']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
