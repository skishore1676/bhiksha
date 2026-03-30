"""Continuous live-loop runtime.

This command warms the configured symbols, then polls Schwab for newly closed
1-minute bars and evaluates the active deployments. In dry-run mode it only
logs plans; in live mode it can place orders through Public.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import polars as pl

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.execution.position_monitor import PositionMonitor
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.market_data.bar_store import RollingBarStore
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.state.reconciliation import reconcile_public_positions


def _frame_from_bars(symbol: str, bars) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol for _ in bars],
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )


async def _run(max_bars: int | None, live: bool) -> None:
    runtime = build_runtime()
    report = await runtime.health_report()
    for item in report.provider_health:
        print(f"HEALTH {item.name} ok={item.ok} detail={item.detail}")
    unhealthy = [item for item in report.provider_health if not item.ok]
    if unhealthy:
        names = ",".join(item.name for item in unhealthy)
        raise RuntimeError(f"Startup health check failed for: {names}")

    symbols = sorted({deployment.symbol for deployment in runtime.enabled_deployments})
    deployments_by_symbol = {symbol: [] for symbol in symbols}
    deployments_by_id = {}
    for deployment in runtime.enabled_deployments:
        deployments_by_symbol[deployment.symbol].append(deployment)
        deployments_by_id[deployment.deployment_id] = deployment

    store = RollingBarStore(max_bars_per_symbol=runtime.app_config.rolling_bar_capacity)
    evaluator = ReplaySignalEvaluator(FeatureService(), runtime.strategy_registry)
    supervisor = ExecutionSupervisor(
        event_repository=SQLiteEventRepository(runtime.app_config.sqlite_path),
        app_config=runtime.app_config,
    )
    position_monitor = PositionMonitor(evaluator, supervisor.planner.position_tracker)
    broker = supervisor.planner.order_manager.broker

    portfolio = await broker.get_portfolio()
    tracker_positions = reconcile_public_positions(
        portfolio.get("positions", []),
        runtime.enabled_deployments,
        orders=portfolio.get("orders", []),
    )
    supervisor.planner.position_tracker.replace_positions(tracker_positions)
    print(f"SYNC positions={len(tracker_positions)}")

    for symbol in symbols:
        warmed = await runtime.warm_start_symbol(symbol)
        store.extend(symbol, warmed)
        print(f"WARMED {symbol} bars={len(warmed)}")

    if max_bars == 0:
        print("Stopping after warm start because --max-bars=0")
        return

    try:
        source = SchwabBarSource(poll_interval_seconds=runtime.app_config.bar_poll_interval_seconds)
        seen = 0
        print("Waiting for newly closed 1-minute bars...")
        async for bar in source.stream_closed_bars(symbols):
            latest = store.latest(bar.symbol)
            if latest is not None and latest.timestamp >= bar.timestamp:
                continue
            store.append(bar)
            seen += 1
            print(f"BAR {bar.symbol} {bar.timestamp.isoformat()} close={bar.close}")
            portfolio = await broker.get_portfolio()
            tracker_positions = reconcile_public_positions(
                portfolio.get("positions", []),
                runtime.enabled_deployments,
                orders=portfolio.get("orders", []),
            )
            supervisor.planner.position_tracker.replace_positions(tracker_positions)
            if tracker_positions:
                joined = ",".join(
                    f"{position.deployment_id}:{position.option_symbol}:{position.quantity}"
                    for position in tracker_positions
                )
                print(f"SYNC positions={joined}")

            closed = await supervisor.close_due_positions(
                deployments_by_id,
                now=bar.timestamp,
                dry_run=not live,
            )
            for plan in closed:
                print(f"HARD_FLAT {plan.deployment_id} option={plan.option_symbol} order_id={plan.order_id}")
            frame = _frame_from_bars(bar.symbol, store.get(bar.symbol))
            exited_deployments: set[str] = set()
            exit_evaluations = position_monitor.evaluate_symbol(bar.symbol, frame, deployments_by_id)
            for evaluation in exit_evaluations:
                print(
                    f"{evaluation.deployment.deployment_id}: exit={evaluation.decision.exit} "
                    f"action={evaluation.decision.action} reasons={evaluation.decision.reason}"
                )
                if evaluation.decision.exit:
                    exit_plan = await supervisor.handle_exit(
                        evaluation.deployment,
                        evaluation.position,
                        evaluation.decision,
                        dry_run=not live,
                    )
                    print(f"{evaluation.deployment.deployment_id}: exit_plan={exit_plan}")
                    exited_deployments.add(evaluation.deployment.deployment_id)
            for deployment in deployments_by_symbol[bar.symbol]:
                if deployment.deployment_id in exited_deployments:
                    print(f"{deployment.deployment_id}: entry_skipped_after_exit")
                    continue
                decision = evaluator.evaluate_entry(deployment, frame)
                print(
                    f"{deployment.deployment_id}: signal={decision.signal} "
                    f"direction={decision.direction.value if decision.direction else None} "
                    f"reasons={decision.reason}"
                )
                if decision.signal:
                    plan = await supervisor.handle_signal(deployment, decision, dry_run=not live)
                    print(f"{deployment.deployment_id}: plan={plan}")
            if max_bars is not None and seen >= max_bars:
                print(f"Stopping after {seen} newly closed bars")
                break
    finally:
        await supervisor.close()
        if 'source' in locals():
            await source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Bhiksha continuous live loop")
    parser.add_argument("--max-bars", type=int, default=None, help="Stop after this many newly closed bars")
    parser.add_argument("--live", action="store_true", help="Allow live order submission instead of dry-run planning")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.max_bars, args.live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
