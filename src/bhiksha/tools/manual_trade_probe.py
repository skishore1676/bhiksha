"""Guarded one-off manual trade probe for live pipeline testing."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from bhiksha.app.bootstrap import build_runtime
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionSelectionRequest
from bhiksha.execution.order_manager import OrderManager
from bhiksha.execution.pricing import select_entry_limit
from bhiksha.options.public_chain import PublicOptionChainService
from bhiksha.options.vehicle_resolver import VehicleResolver
from bhiksha.persistence.sqlite import SQLiteEventRepository


async def _run(symbol: str, quantity: int, live: bool, confirm_live: str | None) -> int:
    runtime = build_runtime()
    deployment = next((d for d in runtime.enabled_deployments if d.symbol == symbol), None)
    if deployment is None:
        raise ValueError(f"No enabled deployment found for symbol={symbol}")

    direction = SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower())
    now = datetime.now(UTC)
    selection_request = OptionSelectionRequest(
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=direction,
        signal_timestamp=now,
        execution_profile=deployment.execution.profile,
        execution_params={
            **deployment.execution.model_dump(),
            "long_signal_contract_type": deployment.execution.option_mapping.get("long_signal", "CALL"),
            "short_signal_contract_type": deployment.execution.option_mapping.get("short_signal", "PUT"),
        },
    )

    repo = SQLiteEventRepository(runtime.app_config.sqlite_path)
    chain_service = PublicOptionChainService()
    resolver = VehicleResolver()
    order_manager = OrderManager()
    try:
        contracts = await chain_service.get_chain(
            deployment.symbol,
            contract_type="ALL",
            from_date=now.date(),
            to_date=(now + timedelta(days=deployment.execution.dte_max + 1)).date(),
        )
        selection = resolver.resolve(selection_request, contracts)
        quote = await order_manager.get_option_quote(selection.option_symbol)
        pricing = select_entry_limit(quote, deployment.execution.model_dump())
        if pricing.block_reasons or pricing.limit_price is None:
            raise ValueError(f"No usable Public entry price for {selection.option_symbol}: {pricing.block_reasons}")
        entry_price = pricing.limit_price
        preflight = await order_manager.preflight_entry(selection.option_symbol, entry_price, quantity)

        await repo.append(
            "manual_trade_probe",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "direction": direction.value,
                "quantity": quantity,
                "option_symbol": selection.option_symbol,
                "quote_bid": quote.bid,
                "quote_ask": quote.ask,
                "quote_last": quote.last,
                "quote_open_interest": quote.open_interest,
                "quote_spread_pct": quote.spread_pct,
                "pricing_evidence": {
                    **pricing.evidence(),
                    "preflight_limit_price": preflight.payload["limitPrice"],
                    "preflight_increment": preflight.current_increment,
                },
                "preflight_limit_price": preflight.payload["limitPrice"],
                "preflight_buying_power_requirement": preflight.buying_power_requirement,
                "preflight_estimated_cost": preflight.estimated_cost,
                "preflight_increment": preflight.current_increment,
                "live_requested": live,
            },
        )

        print(f"deployment={deployment.deployment_id}")
        print(f"symbol={deployment.symbol}")
        print(f"direction={direction.value}")
        print(f"option_symbol={selection.option_symbol}")
        print(f"dte={selection.dte}")
        print(f"delta={selection.abs_delta}")
        print(f"quote_bid={quote.bid}")
        print(f"quote_ask={quote.ask}")
        print(f"quote_last={quote.last}")
        print(f"quote_open_interest={quote.open_interest}")
        print(f"quote_spread_pct={quote.spread_pct}")
        print(f"pricing_mode={pricing.policy.mode}")
        print(f"selected_limit_price={pricing.limit_price}")
        print(f"preflight_limit_price={preflight.payload['limitPrice']}")
        print(f"preflight_buying_power_requirement={preflight.buying_power_requirement}")
        print(f"preflight_estimated_cost={preflight.estimated_cost}")

        if not live:
            print("LIVE_SUBMISSION=skipped")
            return 0

        if confirm_live != "YES":
            raise ValueError("Refusing live submission without --confirm-live YES")

        submit_price = float(preflight.payload["limitPrice"])
        result = await order_manager.place_entry_order(selection.option_symbol, submit_price, quantity)
        await repo.append(
            "manual_trade_submission",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "direction": direction.value,
                "quantity": quantity,
                "option_symbol": selection.option_symbol,
                "submit_price": submit_price,
                "order_id": result.order_id,
                "error": result.error,
            },
        )
        print(f"entry_order_id={result.order_id}")
        print(f"entry_error={result.error}")
        if result.order_id is None:
            return 1

        filled, payload, error = await order_manager.wait_for_fill(
            result.order_id,
            timeout_seconds=runtime.app_config.order_fill_timeout_seconds,
            poll_seconds=runtime.app_config.order_fill_poll_seconds,
        )
        await repo.append(
            "manual_trade_fill_check",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "order_id": result.order_id,
                "filled": filled,
                "error": error,
                "payload": payload or {},
            },
        )
        print(f"filled={filled}")
        print(f"fill_error={error}")
        if not filled:
            return 1

        fill_price = _filled_entry_price(payload, fallback=submit_price)
        stop_loss_pct = deployment.exit.stop_loss_pct or deployment.risk.stop_loss_pct
        stop_price = fill_price * (1.0 - stop_loss_pct)
        stop_result = await order_manager.place_stop_loss_order(selection.option_symbol, stop_price, quantity)
        await repo.append(
            "manual_trade_stop_submission",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "option_symbol": selection.option_symbol,
                "stop_price": stop_price,
                "fill_price": fill_price,
                "stop_order_id": stop_result.order_id,
                "stop_error": stop_result.error,
            },
        )
        print(f"stop_order_id={stop_result.order_id}")
        print(f"stop_error={stop_result.error}")
        print(f"stop_price={stop_price}")
        return 0 if stop_result.order_id else 1
    finally:
        await chain_service.close()
        await order_manager.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a guarded one-off manual trade probe")
    parser.add_argument("--symbol", default="QQQ", help="Deployment symbol to probe")
    parser.add_argument("--quantity", type=int, default=1, help="Contracts to submit")
    parser.add_argument("--live", action="store_true", help="Actually submit the entry order")
    parser.add_argument("--confirm-live", default=None, help='Required exact value: "YES"')
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.symbol.upper(), args.quantity, args.live, args.confirm_live))


def _filled_entry_price(payload: dict | None, *, fallback: float) -> float:
    if not payload:
        return fallback
    for key in ("averageFillPrice", "averagePrice", "filledPrice", "price"):
        try:
            value = payload.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return fallback


if __name__ == "__main__":
    raise SystemExit(main())
