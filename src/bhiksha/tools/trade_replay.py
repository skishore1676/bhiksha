"""Inspect recent historical entries paired with replayable exits."""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.replay import ReplaySignalEvaluator, ReplayTrade
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.market_data.session import ET, ensure_utc
from bhiksha.market_data.trading_calendar import trading_window_start

CT = ZoneInfo("America/Chicago")


async def _run(
    *,
    deployment_ids: list[str],
    trading_days: int,
    provider: str | None,
    active_plan: str | None,
    csv_path: str | None,
) -> None:
    runtime = build_runtime(active_plan_path=active_plan)
    evaluator = ReplaySignalEvaluator(FeatureService(), runtime.strategy_registry)
    deployments = _select_deployments(runtime, deployment_ids)
    window_start = trading_window_start(datetime.now(UTC), trading_days)
    csv_rows: list[dict[str, str]] = []

    for deployment in deployments:
        bars = await runtime.warm_start_symbol(deployment.symbol, provider=provider)
        if not bars:
            print(f"DEPLOYMENT={deployment.deployment_id} SYMBOL={deployment.symbol} NO_BARS=true")
            continue
        frame = _frame_from_bars(bars)
        enriched = evaluator.prepare_enriched_frames(frame, [deployment])[deployment.deployment_id]
        start_index = _start_index_for_window(enriched["timestamp"].to_list(), window_start)
        trades = evaluator.scan_trade_history_on_enriched(deployment, enriched, start_at=start_index)
        print(f"DEPLOYMENT={deployment.deployment_id} SYMBOL={deployment.symbol} TRADES={len(trades)}")
        if not trades:
            continue
        for trade in trades:
            print(_format_trade(trade, enriched))
            csv_rows.append(_trade_to_csv_row(trade, enriched))

    if csv_path is not None:
        written = _write_csv(Path(csv_path), csv_rows)
        print(f"CSV_WRITTEN={written} ROWS={len(csv_rows)}")


def _select_deployments(runtime, deployment_ids: list[str]):
    if not deployment_ids:
        return runtime.enabled_deployments
    requested = set(deployment_ids)
    deployments = [deployment for deployment in runtime.deployments if deployment.deployment_id in requested]
    missing = requested - {deployment.deployment_id for deployment in deployments}
    if missing:
        raise ValueError(f"Unknown deployment ids: {sorted(missing)}")
    return deployments


def _frame_from_bars(bars) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [bar.symbol for bar in bars],
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )


def _start_index_for_window(timestamps, window_start) -> int:
    for index, timestamp in enumerate(timestamps):
        if ensure_utc(timestamp) >= window_start:
            return index
    return len(timestamps)


def _timestamp_fields(timestamp) -> tuple[str, str, str]:
    timestamp_utc = ensure_utc(timestamp)
    timestamp_et = timestamp_utc.astimezone(ET).isoformat()
    timestamp_ct = timestamp_utc.astimezone(CT)
    return timestamp_et, timestamp_ct.strftime("%Y-%m-%d"), timestamp_ct.strftime("%I:%M:%S %p %Z")


def _format_trade(trade: ReplayTrade, enriched: pl.DataFrame) -> str:
    payload = _trade_to_row(trade, enriched)
    return json.dumps(payload, default=str, sort_keys=True)


def _outcome_fields(
    entry_direction: str,
    entry_close: float | None,
    exit_close: float | None,
) -> tuple[float | None, float | None, str]:
    if entry_close is None or exit_close is None:
        return None, None, "open"

    underlying_move = round(exit_close - entry_close, 6)
    underlying_move_pct = round(underlying_move / entry_close, 6) if entry_close else None

    if entry_direction == "short":
        if underlying_move < 0:
            thesis_outcome = "favorable"
        elif underlying_move > 0:
            thesis_outcome = "unfavorable"
        else:
            thesis_outcome = "flat"
    elif entry_direction == "long":
        if underlying_move > 0:
            thesis_outcome = "favorable"
        elif underlying_move < 0:
            thesis_outcome = "unfavorable"
        else:
            thesis_outcome = "flat"
    else:
        thesis_outcome = "unknown"

    return underlying_move, underlying_move_pct, thesis_outcome


def _trade_to_row(trade: ReplayTrade, enriched: pl.DataFrame) -> dict[str, object]:
    entry_bar = enriched.row(trade.entry_index, named=True)
    entry_timestamp_et, entry_date_ct, entry_time_ct = _timestamp_fields(trade.entry_decision.timestamp)
    entry_direction = trade.entry_decision.direction.value if trade.entry_decision.direction else ""
    entry_close = entry_bar.get("close")
    row: dict[str, object] = {
        "deployment_id": trade.entry_decision.deployment_id,
        "symbol": trade.entry_decision.symbol,
        "entry_timestamp_et": entry_timestamp_et,
        "entry_date_ct": entry_date_ct,
        "entry_time_ct": entry_time_ct,
        "entry_direction": entry_direction,
        "entry_bar_open": entry_bar.get("open"),
        "entry_bar_close": entry_close,
        "entry_reason": trade.entry_decision.reason,
        "exit_category": trade.exit_category,
        "premium_exit_status": trade.premium_exit_status,
    }
    if trade.exit_decision is None or trade.exit_index is None:
        underlying_move, underlying_move_pct, thesis_outcome = _outcome_fields(entry_direction, entry_close, None)
        row.update(
            {
                "exit_timestamp_et": "",
                "exit_date_ct": "",
                "exit_time_ct": "",
                "exit_bar_open": "",
                "exit_bar_close": "",
                "exit_action": "",
                "exit_reason": [],
                "underlying_move": underlying_move,
                "underlying_move_pct": underlying_move_pct,
                "thesis_outcome": thesis_outcome,
                "holding_bars": "",
            }
        )
        return row

    exit_bar = enriched.row(trade.exit_index, named=True)
    exit_timestamp_et, exit_date_ct, exit_time_ct = _timestamp_fields(trade.exit_decision.timestamp)
    exit_close = exit_bar.get("close")
    underlying_move, underlying_move_pct, thesis_outcome = _outcome_fields(entry_direction, entry_close, exit_close)
    row.update(
        {
            "exit_timestamp_et": exit_timestamp_et,
            "exit_date_ct": exit_date_ct,
            "exit_time_ct": exit_time_ct,
            "exit_bar_open": exit_bar.get("open"),
            "exit_bar_close": exit_close,
            "exit_action": trade.exit_decision.action,
            "exit_reason": trade.exit_decision.reason,
            "underlying_move": underlying_move,
            "underlying_move_pct": underlying_move_pct,
            "thesis_outcome": thesis_outcome,
            "holding_bars": str(trade.exit_index - trade.entry_index),
        }
    )
    return row


def _trade_to_csv_row(trade: ReplayTrade, enriched: pl.DataFrame) -> dict[str, str]:
    raw = _trade_to_row(trade, enriched)
    row = {key: ("" if value is None else str(value)) for key, value in raw.items()}
    row["entry_reason_json"] = json.dumps(raw["entry_reason"])
    row["exit_reason_json"] = json.dumps(raw["exit_reason"])
    del row["entry_reason"]
    del row["exit_reason"]
    return row


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "deployment_id",
        "symbol",
        "entry_timestamp_et",
        "entry_date_ct",
        "entry_time_ct",
        "entry_direction",
        "entry_bar_open",
        "entry_bar_close",
        "entry_reason_json",
        "exit_category",
        "premium_exit_status",
        "exit_timestamp_et",
        "exit_date_ct",
        "exit_time_ct",
        "exit_bar_open",
        "exit_bar_close",
        "exit_action",
        "exit_reason_json",
        "underlying_move",
        "underlying_move_pct",
        "thesis_outcome",
        "holding_bars",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay entry signals with strategy/time exits")
    parser.add_argument(
        "--deployment-id",
        action="append",
        dest="deployment_ids",
        default=[],
        help="Deployment id to inspect. Can be repeated. Defaults to enabled deployments.",
    )
    parser.add_argument("--trading-days", type=int, default=3, help="How many recent NYSE trading days to inspect")
    parser.add_argument("--provider", default=None, help="Override warm-start provider")
    parser.add_argument(
        "--active-plan",
        default=None,
        help="Path to an active plan JSON. When supplied, replay ignores config/deployments.",
    )
    parser.add_argument("--csv", default=None, help="Write replayed trades to a CSV file")
    args = parser.parse_args(argv)

    asyncio.run(
        _run(
            deployment_ids=args.deployment_ids,
            trading_days=args.trading_days,
            provider=args.provider,
            active_plan=args.active_plan,
            csv_path=args.csv,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
