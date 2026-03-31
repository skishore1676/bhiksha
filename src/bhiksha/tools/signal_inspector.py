"""Inspect recent historical signal triggers for configured deployments."""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime
import json
from pathlib import Path

import polars as pl

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.market_data.session import ET, ensure_utc
from bhiksha.market_data.trading_calendar import trading_window_start


async def _run(
    *,
    deployment_ids: list[str],
    trading_days: int,
    provider: str | None,
    show_all: bool,
    show_features: bool,
    csv_path: str | None,
) -> None:
    runtime = build_runtime()
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
        decisions = evaluator.scan_entry_history_on_enriched(
            deployment,
            enriched,
            start_at=start_index,
            signals_only=not show_all,
        )
        print(
            f"DEPLOYMENT={deployment.deployment_id} SYMBOL={deployment.symbol} "
            f"WINDOW_START_UTC={window_start.isoformat()} TRADING_DAYS={trading_days} BARS={enriched.height} "
            f"MATCHES={len(decisions)}"
        )
        if not decisions:
            continue
        for decision in decisions:
            print(_format_decision(decision, show_features=show_features))
            csv_rows.append(
                _decision_to_csv_row(
                    decision,
                    show_features=show_features,
                    window_start=window_start,
                    trading_days=trading_days,
                )
            )

    if csv_path is not None:
        written = _write_csv(Path(csv_path), csv_rows, show_features=show_features)
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


def _start_index_for_window(timestamps: list[datetime], window_start: datetime) -> int:
    for index, timestamp in enumerate(timestamps):
        if ensure_utc(timestamp) >= window_start:
            return index
    return len(timestamps)


def _format_decision(decision, *, show_features: bool) -> str:
    timestamp_utc = ensure_utc(decision.timestamp)
    timestamp_et = timestamp_utc.astimezone(ET)
    payload = {
        "timestamp_et": timestamp_et.isoformat(),
        "timestamp_utc": timestamp_utc.isoformat(),
        "deployment_id": decision.deployment_id,
        "symbol": decision.symbol,
        "signal": decision.signal,
        "direction": decision.direction.value if decision.direction else None,
        "reason": decision.reason,
    }
    if show_features:
        payload["features"] = decision.features
    return json.dumps(payload, default=str, sort_keys=True)


def _decision_to_csv_row(decision, *, show_features: bool, window_start: datetime, trading_days: int) -> dict[str, str]:
    timestamp_utc = ensure_utc(decision.timestamp)
    timestamp_et = timestamp_utc.astimezone(ET)
    row = {
        "deployment_id": decision.deployment_id,
        "symbol": decision.symbol,
        "timestamp_et": timestamp_et.isoformat(),
        "timestamp_utc": timestamp_utc.isoformat(),
        "signal": str(decision.signal),
        "direction": decision.direction.value if decision.direction else "",
        "reason_json": json.dumps(decision.reason),
        "window_start_utc": window_start.isoformat(),
        "trading_days": str(trading_days),
    }
    if show_features:
        row["features_json"] = json.dumps(decision.features, default=str, sort_keys=True)
    return row


def _write_csv(path: Path, rows: list[dict[str, str]], *, show_features: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "deployment_id",
        "symbol",
        "timestamp_et",
        "timestamp_utc",
        "signal",
        "direction",
        "reason_json",
        "window_start_utc",
        "trading_days",
    ]
    if show_features:
        fieldnames.append("features_json")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect recent historical signal triggers for deployments")
    parser.add_argument(
        "--deployment-id",
        action="append",
        dest="deployment_ids",
        default=[],
        help="Deployment id to inspect. Can be repeated. Defaults to enabled deployments.",
    )
    parser.add_argument("--trading-days", type=int, default=3, help="How many recent NYSE trading days to inspect")
    parser.add_argument("--provider", default=None, help="Override warm-start provider")
    parser.add_argument("--show-all", action="store_true", help="Print every evaluated bar, not just signal=True")
    parser.add_argument("--show-features", action="store_true", help="Include decision features in the output")
    parser.add_argument("--csv", default=None, help="Write matching rows to a CSV file")
    args = parser.parse_args(argv)

    asyncio.run(
        _run(
            deployment_ids=args.deployment_ids,
            trading_days=args.trading_days,
            provider=args.provider,
            show_all=args.show_all,
            show_features=args.show_features,
            csv_path=args.csv,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
