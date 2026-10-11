"""Build the broker-inert strategy/exit JSON feed for an Excel renderer."""
import argparse
from datetime import date
import json
from pathlib import Path
from bhiksha.ops.strategy_exit_report import extract, summarize, attach_inferences, report_digest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--db-path")
    source.add_argument("--report-json", help="Attach inferences to this existing evidence snapshot without rereading live stores")
    p.add_argument("--exit-db-path")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--output", required=True)
    p.add_argument("--inferences")
    a = p.parse_args(argv)
    if a.db_path:
        if not all((a.exit_db_path, a.start, a.end)):
            p.error("--db-path requires --exit-db-path, --start and --end")
        report = summarize(extract(a.db_path, a.exit_db_path, a.start, a.end))
    else:
        report = json.loads(Path(a.report_json).read_text())
    if a.inferences:
        attach_inferences(report, json.loads(Path(a.inferences).read_text()))
    target = Path(a.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": "ok", "rows": len(report["rows"]), "trades": len(report["trades"]),
                      "exit_results": len(report["exits"]), "report_digest": report_digest(report)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
