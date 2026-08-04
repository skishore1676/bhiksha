"""Export one broker-inert chart-scenario cycle from Schwab market data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from bhiksha.chart_scenarios.models import as_utc
from bhiksha.chart_scenarios.paths import require_experiment_path, run_artifact_paths
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.validation import read_installed_plan
from bhiksha.config.environment import load_dotenv
from bhiksha.integrations.schwab.chain import SchwabOptionChainService
from bhiksha.integrations.schwab.market_data_client import (
    SchwabReadOnlyMarketDataClient,
)
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.ops.chart_scenario_live_export import export_live_cycle_input


async def _run(args: argparse.Namespace) -> dict:
    plan = read_installed_plan(args.plan)
    paths = run_artifact_paths(
        str(plan.run_manifest["campaign_id"]), str(plan.run_manifest["run_id"])
    )
    repository = ScenarioEventRepository(args.db_path or paths.database)
    client = SchwabReadOnlyMarketDataClient()
    # Pass the already-stripped settings explicitly. SchwabBarSource's general
    # constructor otherwise loads the repo dotenv for the live trading client.
    bar_source = SchwabBarSource(client=client, settings=client.settings)
    chain_service = SchwabOptionChainService(client=client)
    try:
        payload = await export_live_cycle_input(
            plan,
            repository=repository,
            bar_source=bar_source,
            chain_service=chain_service,
            quote_client=client,
            evaluated_at=as_utc(args.at) if args.at else None,
            observation_slot_ordinal=args.observation_slot,
        )
    finally:
        await client.close()
    _write_atomic(Path(args.output or paths.live_cycle_input), payload)
    return payload


def _write_atomic(path: Path, payload: dict) -> None:
    target = require_experiment_path(path, role="live cycle export")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    if os.getenv("BHIKSHA_SANITIZED_SUBPROCESS") != "1":
        load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan", default="artifacts/chart_scenarios/active_shadow_plan.json"
    )
    parser.add_argument("--db-path")
    parser.add_argument("--output")
    parser.add_argument("--at", type=lambda value: datetime.fromisoformat(value))
    parser.add_argument("--observation-slot", type=int)
    args = parser.parse_args(argv)
    payload = asyncio.run(_run(args))
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
