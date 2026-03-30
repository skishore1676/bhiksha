"""Dry-run runtime entry point.

This command does not place orders. It warms bars for the enabled deployments
and evaluates the latest signal state using real provider data.
"""

from __future__ import annotations

import asyncio
import argparse

import polars as pl

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.persistence.sqlite import SQLiteEventRepository


async def _run(live: bool) -> None:
    runtime = build_runtime()
    evaluator = ReplaySignalEvaluator(FeatureService(), runtime.strategy_registry)
    supervisor = ExecutionSupervisor(
        event_repository=SQLiteEventRepository(runtime.app_config.sqlite_path),
        app_config=runtime.app_config,
    )

    try:
        for deployment in runtime.enabled_deployments:
            bars = await runtime.warm_start_symbol(deployment.symbol)
            if not bars:
                print(f"{deployment.deployment_id}: no bars available")
                continue

            frame = pl.DataFrame(
                {
                    "timestamp": [bar.timestamp for bar in bars],
                    "open": [bar.open for bar in bars],
                    "high": [bar.high for bar in bars],
                    "low": [bar.low for bar in bars],
                    "close": [bar.close for bar in bars],
                    "volume": [bar.volume for bar in bars],
                }
            )
            decision = evaluator.evaluate_entry(deployment, frame)
            print(
                f"{deployment.deployment_id}: signal={decision.signal} "
                f"direction={decision.direction.value if decision.direction else None} "
                f"reasons={decision.reason}"
            )
            if decision.signal:
                plan = await supervisor.handle_signal(deployment, decision, dry_run=not live)
                print(f"{deployment.deployment_id}: plan={plan}")
    finally:
        await supervisor.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warm-start evaluation for active deployments")
    parser.add_argument("--live", action="store_true", help="Allow live order submission instead of dry-run planning")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
