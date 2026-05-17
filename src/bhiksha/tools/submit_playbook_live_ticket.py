"""Submit an approved playbook live ticket into Bhiksha's managed lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bhiksha.execution.order_manager import OrderManager
from bhiksha.packets.playbook_lifecycle import submit_playbook_live_ticket
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.state.lifecycle import TradeLifecycleStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-ticket-artifact", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db-path", default="bhiksha.db")
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/lifecycle"))
    parser.add_argument("--fill-timeout-seconds", type=int, default=20)
    parser.add_argument("--fill-poll-seconds", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    backend = SQLiteBackend(args.db_path)
    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=args.live_ticket_artifact,
            packet_path=args.packet,
            order_manager=OrderManager(),
            event_repository=SQLiteEventRepository(args.db_path, backend=backend),
            trade_state_repository=SQLiteTradeStateRepository(args.db_path, backend=backend),
            lifecycle_store=TradeLifecycleStore(),
            out_root=args.out_root,
            fill_timeout_seconds=args.fill_timeout_seconds,
            fill_poll_seconds=args.fill_poll_seconds,
        )
    )
    payload = {
        "status": result.status,
        "lifecycle_started": result.lifecycle_started,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "symbol": result.symbol,
        "direction": result.direction,
        "option_symbol": result.option_symbol,
        "quantity": result.quantity,
        "entry_order_id": result.entry_order_id,
        "stop_order_id": result.stop_order_id,
        "stop_price": result.stop_price,
        "target_order_id": result.target_order_id,
        "target_price": result.target_price,
        "emergency_exit_order_id": result.emergency_exit_order_id,
        "management_policy_id": result.management_policy_id,
        "trade_state": result.trade_state,
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
    return 0 if result.status in {"lifecycle_started", "pending_entry_reconcile"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
