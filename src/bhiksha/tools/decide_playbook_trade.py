"""Record a take/watch/pass operator decision for a Bhiksha playbook consultation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.packets.operator_decision import record_playbook_operator_decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consultation-artifact", type=Path, required=True)
    parser.add_argument("--decision", choices=["take", "watch", "pass"], required=True)
    parser.add_argument("--operator-note", required=True)
    parser.add_argument("--selected-management-policy")
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/intents"))
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    result = record_playbook_operator_decision(
        consultation_artifact=args.consultation_artifact,
        decision=args.decision,
        operator_note=args.operator_note,
        selected_management_policy_id=args.selected_management_policy,
        out_root=args.out_root,
    )
    payload = {
        "status": result.status,
        "decision": result.decision,
        "execution_ready": result.execution_ready,
        "execution_mode": result.execution_mode,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "symbol": result.symbol,
        "direction": result.direction,
        "timestamp": result.timestamp,
        "mala_verdict": result.mala_verdict,
        "mala_policy": result.mala_policy,
        "selected_management_policy_id": result.selected_management_policy_id,
        "warning_reasons": result.warning_reasons,
        "block_reasons": result.block_reasons,
        "order_submission_allowed": result.order_submission_allowed,
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
    return 0 if result.status != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
