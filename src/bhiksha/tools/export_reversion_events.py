"""Export Bhiksha mean-reversion signal events for parity comparison."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.intraday_mean_reversion import IntradayMeanReversionStrategy


def export_reversion_events(
    *,
    bars_path: Path | None = None,
    data_dir: Path | None = None,
    config_path: Path,
    out_path: Path,
    assume_enriched: bool = False,
    symbols: list[str] | None = None,
) -> Path:
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    configs = config_payload.get("configs") or {}
    if not isinstance(configs, dict):
        raise ValueError("config payload must contain a configs mapping")
    active_frames = _load_active_frames(
        bars_path=bars_path,
        data_dir=data_dir,
        config_payload=config_payload,
        symbols=symbols,
    )

    strategy = IntradayMeanReversionStrategy()
    rows: list[dict[str, Any]] = []
    for _, bars in active_frames:
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
                        "policy_id": config_id,
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
        return _normalize_timestamp(pl.read_parquet(path))
    frame = pl.read_csv(path, try_parse_dates=True)
    if "timestamp" in frame.columns and frame["timestamp"].dtype == pl.String:
        frame = frame.with_columns(pl.col("timestamp").str.to_datetime())
    return _normalize_timestamp(frame)


def _load_active_frames(
    *,
    bars_path: Path | None,
    data_dir: Path | None,
    config_payload: dict[str, Any],
    symbols: list[str] | None,
) -> list[tuple[str, pl.DataFrame]]:
    if bars_path is not None:
        frame = _normalize_symbol_column(_load_bars(bars_path))
        symbol = str(frame["symbol"][0]).upper() if "symbol" in frame.columns and frame.height else ""
        return [(symbol, frame)]
    if data_dir is None:
        raise ValueError("Either bars_path or data_dir is required")

    start = date.fromisoformat(str(config_payload["start"]))
    end = date.fromisoformat(str(config_payload["end"]))
    active_symbols = [str(symbol).upper() for symbol in (symbols or config_payload.get("symbols") or [])]
    frames: list[tuple[str, pl.DataFrame]] = []
    for symbol in active_symbols:
        files = [
            path
            for path in sorted((data_dir / symbol).glob("*.parquet"))
            if start <= date.fromisoformat(path.stem) <= end
        ]
        if not files:
            continue
        frame = pl.concat([_normalize_timestamp(pl.read_parquet(path)) for path in files]).sort("timestamp")
        frame = _normalize_symbol_column(frame, fallback_symbol=symbol)
        frames.append((symbol, frame))
    return frames


def _normalize_timestamp(frame: pl.DataFrame) -> pl.DataFrame:
    if "timestamp" not in frame.columns:
        return frame
    result = frame.with_columns(
        pl.col("timestamp")
        .cast(pl.Int64)
        .cast(pl.Datetime("us", time_zone="UTC"))
        .alias("timestamp")
    )
    float_columns = [column for column in ("open", "high", "low", "close", "volume", "vwap") if column in result.columns]
    if float_columns:
        result = result.with_columns([pl.col(column).cast(pl.Float64).alias(column) for column in float_columns])
    if "transactions" in result.columns:
        result = result.with_columns(pl.col("transactions").cast(pl.Int64).alias("transactions"))
    return result


def _normalize_symbol_column(frame: pl.DataFrame, fallback_symbol: str | None = None) -> pl.DataFrame:
    if "symbol" in frame.columns:
        return frame.with_columns(pl.col("symbol").cast(pl.String).str.to_uppercase())
    if "ticker" in frame.columns:
        return frame.rename({"ticker": "symbol"}).with_columns(
            pl.col("symbol").cast(pl.String).str.to_uppercase()
        )
    if fallback_symbol:
        return frame.with_columns(pl.lit(fallback_symbol.upper()).alias("symbol"))
    return frame


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--assume-enriched", action="store_true")
    parser.add_argument("--symbols", nargs="*")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path = export_reversion_events(
        bars_path=args.bars,
        data_dir=args.data_dir,
        config_path=args.config,
        out_path=args.out,
        assume_enriched=args.assume_enriched,
        symbols=args.symbols,
    )
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
