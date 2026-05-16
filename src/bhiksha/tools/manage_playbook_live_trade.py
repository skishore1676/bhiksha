"""Evaluate and execute live playbook management for an open lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from bhiksha.execution.order_manager import OrderManager
from bhiksha.market_data.adapters.public import PublicBarSource
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.packets.playbook_live_management import manage_playbook_live_trade
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycle-artifact", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db-path", default="bhiksha.db")
    parser.add_argument("--out-root", type=Path, default=Path("artifacts/playbook/live_management"))
    parser.add_argument("--underlying-price", type=float)
    parser.add_argument("--quote-provider", choices=["public", "schwab"])
    parser.add_argument("--current-time", help="ISO timestamp for tests/replay; defaults to now.")
    parser.add_argument("--execute", action="store_true", help="Submit a live exit order when management triggers.")
    parser.add_argument("--loop", action="store_true", help="Keep monitoring until blocked or an exit is submitted.")
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--json", action="store_true", help="Print full result JSON.")
    args = parser.parse_args(argv)

    result = asyncio.run(_run_loop(args) if args.loop else _run(args))
    if not args.loop:
        _print_result(result, as_json=args.json)
    return 0 if result.status not in {"blocked"} else 2


def _print_result(result, *, as_json: bool) -> None:
    payload = {
        "status": result.status,
        "action": result.action,
        "packet_id": result.packet_id,
        "version": result.packet_version,
        "trade_id": result.trade_id,
        "symbol": result.symbol,
        "direction": result.direction,
        "option_symbol": result.option_symbol,
        "current_underlying_price": result.current_underlying_price,
        "underlying_stop_price": result.underlying_stop_price,
        "trigger_reasons": result.trigger_reasons,
        "block_reasons": result.block_reasons,
        "exit_order_id": result.exit_order_id,
        "trade_state": result.trade_state,
        "artifact_json": result.artifact_json,
        "artifact_md": result.artifact_md,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            if isinstance(value, list):
                value = ",".join(value)
            print(f"{key.upper()}={value}")


async def _run_loop(args: argparse.Namespace):
    while True:
        result = await _run(args)
        _print_result(result, as_json=args.json)
        if result.status in {"blocked", "exit_submitted", "exit_would_submit"}:
            return result
        await asyncio.sleep(args.interval_seconds)


async def _run(args: argparse.Namespace):
    lifecycle = json.loads(args.lifecycle_artifact.read_text(encoding="utf-8"))
    symbol = str(lifecycle.get("symbol", "")).upper()
    underlying_price = args.underlying_price
    if underlying_price is None and args.quote_provider:
        underlying_price = await _fetch_underlying_price(symbol, args.quote_provider)
    if underlying_price is None:
        raise SystemExit("--underlying-price or --quote-provider is required")

    backend = SQLiteBackend(args.db_path)
    current_time = _parse_time(args.current_time)
    order_manager = OrderManager()
    try:
        return await manage_playbook_live_trade(
            lifecycle_artifact=args.lifecycle_artifact,
            packet_path=args.packet,
            current_underlying_price=underlying_price,
            order_manager=order_manager,
            event_repository=SQLiteEventRepository(args.db_path, backend=backend),
            trade_state_repository=SQLiteTradeStateRepository(args.db_path, backend=backend),
            current_time=current_time,
            out_root=args.out_root,
            dry_run=not args.execute,
        )
    finally:
        await order_manager.close()


async def _fetch_underlying_price(symbol: str, provider: str) -> float:
    source = PublicBarSource() if provider == "public" else SchwabBarSource()
    try:
        price = await source.fetch_live_price(symbol)
    finally:
        await source.close()
    if price is None:
        raise SystemExit(f"could not fetch live price for {symbol} from {provider}")
    return float(price[0])


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


if __name__ == "__main__":
    raise SystemExit(main())
