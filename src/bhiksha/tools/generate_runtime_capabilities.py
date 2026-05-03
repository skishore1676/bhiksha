"""Generate a verified Bhiksha runtime capability manifest."""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from typing import Any

import polars as pl

from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.strategy.capabilities import (
    DEFAULT_CAPABILITY_MANIFEST_PATH,
    DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH,
    load_capability_manifest,
)
from bhiksha.strategy.registry import default_strategy_registry


PROBE_PARAMS: dict[tuple[str, str], list[dict[str, Any]]] = {
    (
        "market_impulse",
        "cross_reclaim",
    ): [
        {
            "direction": "short",
            "entry_buffer_minutes": 5,
            "entry_window_minutes": 90,
            "regime_timeframe": "1h",
            "vwma_periods": [5, 13, 21],
        }
    ],
    (
        "jerk_pivot_momentum",
        "default",
    ): [
        {
            "direction": "short",
            "jerk_lookback": 12,
            "kinematic_periods_back": 3,
            "use_volume_filter": True,
            "volume_multiplier": 1.0,
            "vpoc_proximity_pct": 0.0015,
        }
    ],
    (
        "opening_drive_classifier",
        "default",
    ): [
        {
            "direction": "short",
            "opening_window_minutes": 15,
            "entry_start_offset_minutes": 25,
            "entry_end_offset_minutes": 120,
            "kinematic_periods_back": 3,
            "use_directional_mass": True,
            "use_jerk_confirmation": False,
            "use_regime_filter": True,
            "regime_timeframe": "5m",
            "use_volume_filter": False,
        }
    ],
    (
        "elastic_band_reversion",
        "default",
    ): [
        {
            "direction": "long",
            "kinematic_periods_back": 5,
            "use_directional_mass": True,
            "use_jerk_confirmation": True,
            "z_score_window": 360,
        }
    ],
}


def generate_runtime_capability_manifest(
    *,
    declared_manifest_path: str | Path = DEFAULT_CAPABILITY_MANIFEST_PATH,
    output_path: str | Path = DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH,
) -> dict[str, Any]:
    declared_path = Path(declared_manifest_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    declared = load_capability_manifest(declared_path)
    strategies = json.loads(json.dumps(declared.get("strategies") or {}))
    probes: list[dict[str, Any]] = []

    for strategy_key, strategy_config in list(strategies.items()):
        if not isinstance(strategy_config, dict):
            continue
        variants = strategy_config.get("variants")
        if not isinstance(variants, dict):
            continue
        for variant, variant_config in list(variants.items()):
            if not isinstance(variant_config, dict):
                continue
            if str(variant_config.get("status") or "").strip().lower() != "supported":
                continue
            probe_rows = _run_variant_probes(strategy_key, variant)
            probes.extend(probe_rows)
            failures = [probe for probe in probe_rows if probe["status"] != "passed"]
            if failures:
                variant_config["status"] = "unsupported"
                variant_config["reason"] = f"runtime_verification_failed:{failures[0]['probe']}"
                variant_config["verification_status"] = "failed"
            else:
                variant_config["reason"] = variant_config.get("reason") or "runtime_verified"
                variant_config["verification_status"] = "verified"

    payload = {
        "version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "bhiksha.tools.generate_runtime_capabilities",
        "git_sha": _git_sha(),
        "source_manifest_path": str(declared_path),
        "source_manifest_version": declared.get("version"),
        "supported_thesis_exit_policies": declared.get("supported_thesis_exit_policies") or [],
        "strategies": strategies,
        "verification": {
            "status": "passed" if all(probe["status"] == "passed" for probe in probes) else "failed",
            "probe_count": len(probes),
            "probes": probes,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _run_variant_probes(strategy_key: str, variant: str) -> list[dict[str, Any]]:
    params_items = PROBE_PARAMS.get((strategy_key, variant), [{}])
    rows = []
    for index, params in enumerate(params_items, start=1):
        rows.append(_feature_contract_probe(strategy_key, variant, params, index))
    if strategy_key == "opening_drive_classifier" and variant == "default":
        rows.append(_opening_drive_regime_semantic_probe())
    return rows


def _feature_contract_probe(strategy_key: str, variant: str, params: dict[str, Any], index: int) -> dict[str, Any]:
    probe_name = f"feature_contract:{strategy_key}.{variant}:{index}"
    try:
        strategy = default_strategy_registry().get(strategy_key)
        required = strategy.required_features(params)
        enriched = PhysicsEngine().enrich_for_features(_sample_ohlcv(), required)
        concrete_required = {feature for feature in required if ":" not in feature}
        missing = sorted(concrete_required - set(enriched.columns))
        if missing:
            raise AssertionError(f"missing_features:{missing}")
        strategy.evaluate_entry(enriched, f"{strategy_key}_{variant}_capability_probe", params)
    except Exception as exc:  # noqa: BLE001
        return _probe_result(probe_name, strategy_key, variant, "failed", str(exc))
    return _probe_result(probe_name, strategy_key, variant, "passed", "ok")


def _opening_drive_regime_semantic_probe() -> dict[str, Any]:
    strategy_key = "opening_drive_classifier"
    variant = "default"
    probe_name = "semantic:opening_drive.regime_filter"
    params = {
        "direction": "long",
        "opening_window_minutes": 25,
        "entry_start_offset_minutes": 30,
        "entry_end_offset_minutes": 120,
        "min_drive_return_pct": 0.0015,
        "breakout_buffer_pct": 0.0,
        "kinematic_periods_back": 1,
        "use_volume_filter": False,
        "use_directional_mass": False,
        "use_jerk_confirmation": False,
        "use_regime_filter": True,
        "regime_timeframe": "5m",
        "enable_continue": True,
        "enable_fail": False,
    }
    try:
        strategy = default_strategy_registry().get(strategy_key)
        neutral = _opening_drive_frame("neutral")
        bullish = _opening_drive_frame("bullish")
        blocked = strategy.evaluate_entry(neutral, "opening_drive_regime_probe", params)
        allowed = strategy.evaluate_entry(bullish, "opening_drive_regime_probe", params)
        if blocked.signal:
            raise AssertionError("neutral_regime_should_block_signal")
        if not allowed.signal:
            raise AssertionError("bullish_regime_should_allow_signal")
    except Exception as exc:  # noqa: BLE001
        return _probe_result(probe_name, strategy_key, variant, "failed", str(exc))
    return _probe_result(probe_name, strategy_key, variant, "passed", "ok")


def _probe_result(probe: str, strategy_key: str, variant: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "probe": probe,
        "strategy_key": strategy_key,
        "strategy_variant": variant,
        "status": status,
        "detail": detail,
    }


def _sample_ohlcv() -> pl.DataFrame:
    rows = 500
    start = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    closes = [100.0 + (idx * 0.02) for idx in range(rows)]
    opens = [closes[0], *closes[:-1]]
    return pl.DataFrame(
        {
            "symbol": ["SPY"] * rows,
            "timestamp": [start + timedelta(minutes=idx) for idx in range(rows)],
            "open": opens,
            "high": [max(open_price, close) + 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "low": [min(open_price, close) - 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000.0 + (idx % 20) * 50.0 for idx in range(rows)],
        }
    )


def _opening_drive_frame(regime: str) -> pl.DataFrame:
    closes = [100.0, 100.15, 100.2, 100.35, 100.4, 100.45, 100.5, 100.55, 100.6, 100.7]
    start = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(len(closes))]
    opens = [closes[0], *closes[:-1]]
    return pl.DataFrame(
        {
            "symbol": ["SPY"] * len(closes),
            "timestamp": timestamps,
            "open": opens,
            "high": [max(open_price, close) + 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "low": [min(open_price, close) - 0.05 for open_price, close in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1000.0] * len(closes),
            "directional_mass": [1.0] * len(closes),
            "impulse_regime_5m": [regime] * len(closes),
        }
    )


def _git_sha() -> str | None:
    with suppress(Exception):
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()  # noqa: S603,S607
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declared-manifest", default=str(DEFAULT_CAPABILITY_MANIFEST_PATH))
    parser.add_argument("--out", default=str(DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH))
    args = parser.parse_args(argv)

    payload = generate_runtime_capability_manifest(
        declared_manifest_path=args.declared_manifest,
        output_path=args.out,
    )
    print(f"RUNTIME_CAPABILITIES={Path(args.out).resolve()}")
    print(f"VERIFICATION_STATUS={payload['verification']['status']}")
    print(f"PROBE_COUNT={payload['verification']['probe_count']}")
    return 0 if payload["verification"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
