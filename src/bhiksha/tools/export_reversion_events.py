"""Export Bhiksha mean-reversion signal events for parity comparison."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import polars as pl

from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.intraday_mean_reversion import IntradayMeanReversionStrategy


def export_reversion_events(
    *,
    bars_path: Path,
    config_path: Path,
    out_path: Path,
    assume_enriched: bool = False,
) -> Path:
    bars = _load_bars(bars_path)
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    configs = config_payload.get("configs") or {}
    if not isinstance(configs, dict):
        raise ValueError("config payload must contain a configs mapping")

    strategy = IntradayMeanReversionStrategy()
    rows: list[dict[str, Any]] = []
    enriched_cache: dict[str, pl.DataFrame] = {}
    for config_id, raw_config in configs.items():
        config = dict(raw_config)
        feature_key = json.dumps(sorted(strategy.required_features(config)))
        if assume_enriched:
            frame = bars
        else:
            if feature_key not in enriched_cache:
                enriched_cache[feature_key] = PhysicsEngine().enrich_for_features(
                    bars,
                    strategy.required_features(config),
                )
            frame = enriched_cache[feature_key]
        signals = strategy.generate_signals(frame, config)
        for row in signals.filter(pl.col("signal")).to_dicts():
            direction = row.get("signal_direction")
            if not direction:
                continue
            timestamp = row["timestamp"]
            rows.append(
                {
                    "config_id": config_id,
                    "symbol": str(row.get("symbol") or "").upper(),
                    "direction": direction,
                    "event_timestamp": timestamp.isoformat()
                    if hasattr(timestamp, "isoformat")
                    else str(timestamp),
                    "event_type": "entry",
                    "policy_id": str(config.get("exit_family", "")),
                    "exit_family": str(config.get("exit_family", "")),
                }
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "config_id",
                "symbol",
                "direction",
                "event_timestamp",
                "event_type",
                "policy_id",
                "exit_family",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def _load_bars(path: Path) -> pl.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pl.read_parquet(path)
    frame = pl.read_csv(path, try_parse_dates=True)
    if "timestamp" in frame.columns and frame["timestamp"].dtype == pl.String:
        frame = frame.with_columns(pl.col("timestamp").str.to_datetime())
    return frame


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--assume-enriched", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path = export_reversion_events(
        bars_path=args.bars,
        config_path=args.config,
        out_path=args.out,
        assume_enriched=args.assume_enriched,
    )
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
