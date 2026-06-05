"""Run a Bhiksha-authorized consultation against a Mala playbook surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.packets.consultation_bridge import consult_mala_playbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", choices=["long", "short"], required=True)
    parser.add_argument(
        "--timestamp",
        required=True,
        help='Operator timestamp, e.g. "2026-05-11 09:40 America/Chicago".',
    )
    parser.add_argument(
        "--chart-read",
        required=True,
        help="Chart-first operator read. Required before Mala is queried.",
    )
    parser.add_argument("--mala-repo", type=Path, default=_default_mala_repo())
    parser.add_argument("--mala-run-dir", type=Path)
    parser.add_argument("--mala-python", type=Path)
    parser.add_argument("--capability-manifest", type=Path)
    parser.add_argument("--legacy-retirement-report", type=Path)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("artifacts/playbook/consultations"),
    )
    parser.add_argument("--mode", default="state-management")
    parser.add_argument(
        "--no-update-mala-log",
        action="store_true",
        help="Do not ask Mala to append/update its consultation log.",
    )
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    result = consult_mala_playbook(
        packet_path=args.packet,
        symbol=args.symbol,
        direction=args.direction,
        timestamp=args.timestamp,
        chart_read=args.chart_read,
        mala_repo=args.mala_repo,
        mala_run_dir=args.mala_run_dir,
        mala_python=args.mala_python,
        capability_manifest_path=args.capability_manifest,
        legacy_retirement_report_path=args.legacy_retirement_report,
        out_root=args.out_root,
        mode=args.mode,
        update_mala_log=not args.no_update_mala_log,
    )

    payload = {
        "status": result.status,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "runtime_mode": result.runtime_mode,
        "symbol": result.symbol,
        "direction": result.direction,
        "timestamp": result.timestamp,
        "verdict": result.verdict,
        "policy": result.policy,
        "selected_exit": result.selected_exit,
        "allowed_management_policy_ids": result.allowed_management_policy_ids,
        "query_review": result.query_review,
        "policy_card": result.policy_card,
        "artifact_json": result.artifact_json,
        "artifact_md": result.artifact_md,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            if isinstance(value, list):
                value = ",".join(value)
            print(f"{key.upper()}={value}")
    return 0


def _default_mala_repo() -> Path:
    sibling = Path.cwd().parent / "mala_v2"
    if sibling.exists():
        return sibling
    return Path("../mala_v2")


if __name__ == "__main__":
    raise SystemExit(main())
