"""Build an approval-gated option preview for a playbook shadow intent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bhiksha.packets.option_preview import build_playbook_option_preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-artifact", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/option_previews"))
    parser.add_argument("--max-trade-premium-usd", type=float, default=300.0)
    parser.add_argument("--dte-min", type=int, default=0)
    parser.add_argument("--dte-max", type=int, default=7)
    parser.add_argument("--target-abs-delta-min", type=float, default=0.20)
    parser.add_argument("--target-abs-delta-max", type=float, default=0.40)
    parser.add_argument("--min-open-interest", type=int, default=100)
    parser.add_argument("--max-bid-ask-spread-pct", type=float, default=0.20)
    parser.add_argument("--underlying-price", type=float)
    parser.add_argument(
        "--underlying-stop-price",
        type=float,
        help="Required for live approval-gated packets; the underlying invalidation level from the playbook.",
    )
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    result = asyncio.run(
        build_playbook_option_preview(
            intent_artifact=args.intent_artifact,
            packet_path=args.packet,
            out_root=args.out_root,
            max_trade_premium_usd=args.max_trade_premium_usd,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            target_abs_delta_min=args.target_abs_delta_min,
            target_abs_delta_max=args.target_abs_delta_max,
            min_open_interest=args.min_open_interest,
            max_bid_ask_spread_pct=args.max_bid_ask_spread_pct,
            underlying_price=args.underlying_price,
            underlying_stop_price=args.underlying_stop_price,
        )
    )
    payload = {
        "status": result.status,
        "preview_ready": result.preview_ready,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "symbol": result.symbol,
        "direction": result.direction,
        "timestamp": result.timestamp,
        "selected_management_policy_id": result.selected_management_policy_id,
        "option_symbol": result.option_symbol,
        "quantity": result.quantity,
        "estimated_entry_price": result.estimated_entry_price,
        "underlying_entry_price": result.underlying_entry_price,
        "underlying_stop_price": result.underlying_stop_price,
        "risk_reasons": result.risk_reasons,
        "block_reasons": result.block_reasons,
        "order_submission_allowed": result.order_submission_allowed,
        "live_approval_required": result.live_approval_required,
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
    return 0 if result.preview_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
