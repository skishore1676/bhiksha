"""Record end-of-day or management-exit PnL for a playbook shadow preview."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bhiksha.execution.order_manager import OrderManager
from bhiksha.packets.shadow_outcome import record_playbook_shadow_outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--option-preview-artifact", type=Path, required=True)
    parser.add_argument("--exit-timestamp", required=True)
    parser.add_argument("--exit-reason", default="end_of_day_mark")
    parser.add_argument("--exit-price", type=float)
    parser.add_argument(
        "--quote-current",
        action="store_true",
        help="Use the current broker quote bid/last/ask as the shadow exit price.",
    )
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/shadow_outcomes"))
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    order_manager = OrderManager() if args.quote_current else None
    result = asyncio.run(
        record_playbook_shadow_outcome(
            option_preview_artifact=args.option_preview_artifact,
            exit_timestamp=args.exit_timestamp,
            exit_reason=args.exit_reason,
            exit_price=args.exit_price,
            order_manager=order_manager,
            out_root=args.out_root,
        )
    )
    payload = {
        "status": result.status,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "symbol": result.symbol,
        "direction": result.direction,
        "option_symbol": result.option_symbol,
        "quantity": result.quantity,
        "entry_price": result.entry_price,
        "exit_price": result.exit_price,
        "exit_reason": result.exit_reason,
        "gross_pnl_usd": result.gross_pnl_usd,
        "pnl_pct": result.pnl_pct,
        "pnl_r": result.pnl_r,
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
    return 0 if result.status == "shadow_outcome_recorded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
