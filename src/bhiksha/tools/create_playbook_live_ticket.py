"""Approve or reject a playbook option preview for the live lane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.packets.live_ticket import APPROVAL_PHRASE, create_playbook_live_ticket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--option-preview-artifact", type=Path, required=True)
    parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--operator-note", required=True)
    parser.add_argument(
        "--approval-phrase",
        help=f"Required for approval. Exact phrase: {APPROVAL_PHRASE}",
    )
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/live_tickets"))
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    result = create_playbook_live_ticket(
        option_preview_artifact=args.option_preview_artifact,
        decision=args.decision,
        operator=args.operator,
        operator_note=args.operator_note,
        approval_phrase=args.approval_phrase,
        out_root=args.out_root,
    )
    payload = {
        "status": result.status,
        "decision": result.decision,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "symbol": result.symbol,
        "direction": result.direction,
        "option_symbol": result.option_symbol,
        "quantity": result.quantity,
        "limit_price": result.limit_price,
        "operator": result.operator,
        "order_submission_allowed": result.order_submission_allowed,
        "live_approval_required": result.live_approval_required,
        "block_reasons": result.block_reasons,
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
    return 0 if result.status in {"live_ticket_approved", "live_ticket_rejected"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
