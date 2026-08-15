"""Narrow price-only observer for Cartographer's immutable research packets.

This module intentionally imports no broker, order, Sheet, option, or active-plan code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

BATCH_SCHEMA = "market_cartographer.daily_recommendation_batch.v1"
FACTS_SCHEMA = "bhiksha.cartographer_market_facts.v1"
OBSERVATION_SCHEMA = "bhiksha.cartographer_shadow_observation.v1"
EFFECT_KEYS = ("broker", "orders", "auth", "sheet", "active_plan", "external_send")


def zero_effects() -> dict[str, bool]:
    return {key: False for key in EFFECT_KEYS}


def canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_batch(batch: Mapping[str, Any]) -> dict[str, Any]:
    if batch.get("schema") != BATCH_SCHEMA or batch.get("mode") != "shadow_research":
        raise ValueError("unsupported Cartographer recommendation batch")
    if batch.get("effects") != zero_effects():
        raise ValueError("Cartographer batch is not zero-effect")
    recommendations = batch.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        raise ValueError("Cartographer batch has no recommendations")
    expected = canonical_hash({key: value for key, value in batch.items() if key != "batch_hash"})
    if batch.get("batch_hash") != expected:
        raise ValueError("Cartographer recommendation batch hash mismatch")
    return dict(batch)


def validate_market_facts(facts: Mapping[str, Any], *, batch_hash: str) -> dict[str, Any]:
    if facts.get("schema") != FACTS_SCHEMA or facts.get("batch_hash") != batch_hash:
        raise ValueError("market facts are not bound to the recommendation batch")
    points = facts.get("points")
    if not isinstance(points, list):
        raise ValueError("market facts points must be a list")
    identities: set[tuple[str, int]] = set()
    for point in points:
        if not isinstance(point, Mapping):
            raise ValueError("market fact point must be an object")
        identity = (str(point.get("symbol") or ""), int(point.get("horizon_sessions") or 0))
        if not identity[0] or identity[1] < 1 or identity in identities:
            raise ValueError("market fact identities must be unique and valid")
        identities.add(identity)
        if float(point.get("entry_price") or 0) <= 0 or float(point.get("exit_price") or 0) <= 0:
            raise ValueError("market fact prices must be positive")
    expected = canonical_hash({key: value for key, value in facts.items() if key != "facts_hash"})
    if facts.get("facts_hash") != expected:
        raise ValueError("market facts hash mismatch")
    return dict(facts)


def build_observation(
    batch: Mapping[str, Any], market_facts: Mapping[str, Any], *, observed_at: str
) -> dict[str, Any]:
    normalized = validate_batch(batch)
    facts = validate_market_facts(market_facts, batch_hash=str(normalized["batch_hash"]))
    points = {
        (str(point["symbol"]), int(point["horizon_sessions"])): point
        for point in facts["points"]
    }
    observations: list[dict[str, Any]] = []
    for recommendation in normalized["recommendations"]:
        for horizon in recommendation["evaluation_horizons_sessions"]:
            base = {
                "recommendation_id": recommendation["recommendation_id"],
                "arm": recommendation["arm"],
                "symbol": recommendation["symbol"],
                "direction": recommendation["direction"],
                "horizon_sessions": int(horizon),
            }
            if recommendation["direction"] == "abstain":
                observations.append(
                    {**base, "status": "abstained", "directional_return": None}
                )
                continue
            point = points.get((recommendation["symbol"], int(horizon)))
            if point is None:
                observations.append(
                    {**base, "status": "pending_market_data", "directional_return": None}
                )
                continue
            raw_return = float(point["exit_price"]) / float(point["entry_price"]) - 1.0
            directional = raw_return if recommendation["direction"] == "long" else -raw_return
            observations.append(
                {
                    **base,
                    "status": "closed",
                    "entry_price": round(float(point["entry_price"]), 8),
                    "entry_at": point["entry_at"],
                    "exit_price": round(float(point["exit_price"]), 8),
                    "exit_at": point["exit_at"],
                    "raw_underlying_return": round(raw_return, 8),
                    "directional_return": round(directional, 8),
                    "source": point.get("source"),
                }
            )
    body: dict[str, Any] = {
        "schema": OBSERVATION_SCHEMA,
        "status": (
            "complete"
            if all(row["status"] != "pending_market_data" for row in observations)
            else "collecting"
        ),
        "observed_at": observed_at,
        "batch_hash": normalized["batch_hash"],
        "run_id": normalized["run_id"],
        "facts_hash": facts["facts_hash"],
        "observations": observations,
        "effects": zero_effects(),
    }
    body["observation_hash"] = canonical_hash(body)
    return body


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _minute_roots(data_root: Path) -> list[Path]:
    root = data_root.expanduser().resolve()
    manifest_path = root / "market-cartographer-overlay.json"
    if not manifest_path.is_file():
        return [root]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    roots = [Path(str(payload["base_minute_root"])).expanduser().resolve()]
    overlay = Path(str(payload["minute_overlay_root"])).expanduser().resolve()
    if overlay.is_dir():
        roots.append(overlay)
    return roots


def _session_files(data_root: Path, symbol: str, after: date) -> list[Path]:
    files: dict[str, Path] = {}
    for root in _minute_roots(data_root):
        symbol_root = root / symbol.upper()
        if not symbol_root.is_dir():
            continue
        for path in symbol_root.glob("????-??-??.parquet"):
            if path.stem > after.isoformat():
                files[path.stem] = path
    return [files[key] for key in sorted(files)]


def _regular_open_close(path: Path) -> tuple[float, float, str, str]:
    session_date = date.fromisoformat(path.stem)
    eastern = ZoneInfo("America/New_York")
    start = datetime.combine(session_date, time(9, 30), eastern)
    end = datetime.combine(session_date, time(16, 0), eastern)
    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(end.timestamp() * 1_000_000)
    frame = (
        pl.read_parquet(path)
        .select(["timestamp", "open", "close"])
        .with_columns(pl.col("timestamp").cast(pl.Int64))
        .filter((pl.col("timestamp") >= start_us) & (pl.col("timestamp") < end_us))
        .sort("timestamp")
    )
    if frame.is_empty():
        raise ValueError(f"{path}: no regular-session rows")
    first = frame.row(0, named=True)
    last = frame.row(-1, named=True)
    entry_at = datetime.fromtimestamp(first["timestamp"] / 1_000_000, tz=ZoneInfo("UTC"))
    exit_at = datetime.fromtimestamp(last["timestamp"] / 1_000_000, tz=ZoneInfo("UTC"))
    return float(first["open"]), float(last["close"]), entry_at.isoformat(), exit_at.isoformat()


def build_market_facts_from_mala(
    batch: Mapping[str, Any], data_root: str | Path
) -> dict[str, Any]:
    normalized = validate_batch(batch)
    source_date = datetime.fromisoformat(str(normalized["as_of"])).astimezone(
        ZoneInfo("America/New_York")
    ).date()
    points: list[dict[str, Any]] = []
    symbols = sorted({str(item["symbol"]) for item in normalized["recommendations"]})
    horizons = sorted(
        {
            int(horizon)
            for item in normalized["recommendations"]
            for horizon in item["evaluation_horizons_sessions"]
        }
    )
    root = Path(data_root)
    for symbol in symbols:
        files = _session_files(root, symbol, source_date)
        if not files:
            continue
        entry_price, _close, entry_at, _exit_at = _regular_open_close(files[0])
        for horizon in horizons:
            if len(files) < horizon:
                continue
            _open, exit_price, _unused, exit_at = _regular_open_close(files[horizon - 1])
            points.append(
                {
                    "symbol": symbol,
                    "horizon_sessions": horizon,
                    "entry_price": round(entry_price, 8),
                    "entry_at": entry_at,
                    "exit_price": round(exit_price, 8),
                    "exit_at": exit_at,
                    "source": {
                        "provider": "mala_parquet_read_only",
                        "entry_file": str(files[0].resolve()),
                        "entry_file_hash": _file_hash(files[0]),
                        "exit_file": str(files[horizon - 1].resolve()),
                        "exit_file_hash": _file_hash(files[horizon - 1]),
                    },
                }
            )
    body: dict[str, Any] = {
        "schema": FACTS_SCHEMA,
        "batch_hash": normalized["batch_hash"],
        "source_as_of": normalized["as_of"],
        "points": points,
        "effects": zero_effects(),
    }
    body["facts_hash"] = canonical_hash(body)
    return body


def write_json(value: Mapping[str, Any], path: str | Path) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def build_terminal_fact(
    *,
    deployment: Mapping[str, Any],
    trade_id: str,
    terminal_reason: str,
    option_excursion: Mapping[str, Any],
    underlying_excursion: Mapping[str, Any],
    gross_pnl_usd: float | None = None,
    net_pnl_usd: float | None = None,
) -> dict[str, Any]:
    """Freeze one local shadow result without inferring missing coverage or economics."""

    source = dict(deployment.get("source") or {})
    metadata = dict(source.get("metadata") or {})
    if metadata.get("source_owner") != "market_cartographer":
        raise ValueError("terminal fact requires a Cartographer-owned deployment")
    required = ("signal_id", "signal_hash", "cartographer_version", "run_id", "profile_slug", "bundle_hash")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise ValueError(f"terminal fact is missing identity: {', '.join(missing)}")
    coverage = {
        "option": str(option_excursion.get("coverage") or "missing"),
        "underlying": str(underlying_excursion.get("coverage") or "missing"),
    }
    decision_ready = all(value == "complete" for value in coverage.values())
    body: dict[str, Any] = {
        "schema": "bhiksha.cartographer_shadow_terminal_fact.v1",
        "status": "closed" if decision_ready else "inconclusive",
        "decision_ready": decision_ready,
        "identity": {
            "signal_id": metadata["signal_id"],
            "signal_hash": metadata["signal_hash"],
            "cartographer_version": metadata["cartographer_version"],
            "run_id": metadata["run_id"],
            "deployment_id": deployment["deployment_id"],
            "trade_id": trade_id,
            "profile_slug": metadata["profile_slug"],
            "bundle_hash": metadata["bundle_hash"],
        },
        "terminal_reason": terminal_reason,
        "option_excursion": dict(option_excursion),
        "underlying_excursion": dict(underlying_excursion),
        "coverage": coverage,
        "gross_pnl_usd": gross_pnl_usd,
        "net_pnl_usd": net_pnl_usd,
        "economics": {
            "gross_pnl_available": gross_pnl_usd is not None,
            "net_pnl_available": net_pnl_usd is not None,
            "excursion_decision_ready": decision_ready,
        },
        "effects": zero_effects(),
    }
    body["fact_receipt_id"] = canonical_hash(body)
    return body


__all__ = [
    "FACTS_SCHEMA",
    "OBSERVATION_SCHEMA",
    "build_market_facts_from_mala",
    "build_terminal_fact",
    "build_observation",
    "canonical_hash",
    "validate_batch",
    "validate_market_facts",
    "write_json",
    "zero_effects",
]
