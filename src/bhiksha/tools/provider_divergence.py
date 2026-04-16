"""Compare Schwab vs Polygon bars and derived Newton features for a symbol."""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from bhiksha.config.environment import load_dotenv
from bhiksha.market_data.adapters.polygon import PolygonBarSource
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.market_data.newton.engine import PhysicsEngine
from bhiksha.market_data.session import ensure_utc
from bhiksha.market_data.trading_calendar import trading_window_start

CT = ZoneInfo("America/Chicago")

OHLCV = ("open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
SESSION_CHOICES = ("all", "regular", "extended")
REGULAR_OPEN_ET = time(9, 30)
REGULAR_CLOSE_ET = time(16, 0)


async def _fetch_bars(symbol: str, start: datetime, end: datetime):
    schwab = SchwabBarSource()
    polygon = PolygonBarSource()
    try:
        schwab_bars, polygon_bars = await asyncio.gather(
            schwab.warm_start(symbol, start, end),
            polygon.warm_start(symbol, start, end),
        )
    finally:
        await schwab.close()
        await polygon.close()
    return schwab_bars, polygon_bars


def _frame_from_bars(bars) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [bar.symbol for bar in bars],
            "timestamp": [ensure_utc(bar.timestamp) for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )


def _align(schwab_df: pl.DataFrame, polygon_df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    schwab_ts = set(schwab_df["timestamp"].to_list())
    polygon_ts = set(polygon_df["timestamp"].to_list())
    common = sorted(schwab_ts & polygon_ts)
    common_set = set(common)
    s_aligned = schwab_df.filter(pl.col("timestamp").is_in(common)).sort("timestamp")
    p_aligned = polygon_df.filter(pl.col("timestamp").is_in(common)).sort("timestamp")
    return s_aligned, p_aligned, len(schwab_ts - common_set), len(polygon_ts - common_set)


def _is_regular_session(timestamp: datetime) -> bool:
    timestamp_et = ensure_utc(timestamp).astimezone(ZoneInfo("America/New_York")).time().replace(tzinfo=None)
    return REGULAR_OPEN_ET <= timestamp_et <= REGULAR_CLOSE_ET


def _session_label(timestamp: datetime) -> str:
    return "regular" if _is_regular_session(timestamp) else "extended"


def _session_allowed(timestamp: datetime, session: str) -> bool:
    if session == "all":
        return True
    label = _session_label(timestamp)
    return label == session


def _filter_session(df: pl.DataFrame, session: str) -> pl.DataFrame:
    if session == "all" or df.is_empty():
        return df
    allowed = [_session_allowed(ensure_utc(ts), session) for ts in df["timestamp"].to_list()]
    return df.filter(pl.Series(allowed))


def _session_counts(df: pl.DataFrame, window_start: datetime) -> dict[str, int]:
    counts = {"all": 0, "regular": 0, "extended": 0}
    if df.is_empty():
        return counts
    for raw_ts in df["timestamp"].to_list():
        ts = ensure_utc(raw_ts)
        if ts < window_start:
            continue
        counts["all"] += 1
        counts[_session_label(ts)] += 1
    return counts


def _pct(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    ref = abs(a) if a != 0 else abs(b)
    return abs(a - b) / ref


def _diff_row(ts: datetime, s_row: dict, p_row: dict, feature_cols: list[str]) -> dict:
    ts_ct = ts.astimezone(CT)
    row: dict = {
        "timestamp": ts.isoformat(),
        "date_ct": ts_ct.strftime("%Y-%m-%d"),
        "time_ct": ts_ct.strftime("%I:%M %p"),
        "session": _session_label(ts),
    }
    for col in OHLCV:
        sv, pv = s_row[col], p_row[col]
        row[f"{col}_schwab"] = sv
        row[f"{col}_polygon"] = pv
        row[f"{col}_diff"] = round(sv - pv, 6)
        row[f"{col}_pct"] = round(_pct(sv, pv), 6) if sv is not None and pv is not None else None

    for col in feature_cols:
        sv = s_row.get(col)
        pv = p_row.get(col)
        s_nan = sv is None or (isinstance(sv, float) and math.isnan(sv))
        p_nan = pv is None or (isinstance(pv, float) and math.isnan(pv))
        if s_nan and p_nan:
            row[f"{col}_diff"] = 0.0
            row[f"{col}_pct"] = 0.0
        elif s_nan or p_nan:
            row[f"{col}_diff"] = None
            row[f"{col}_pct"] = None
        else:
            row[f"{col}_diff"] = round(sv - pv, 6)
            row[f"{col}_pct"] = round(_pct(sv, pv), 6)
    return row


def _enrich(df: pl.DataFrame) -> pl.DataFrame:
    engine = PhysicsEngine()
    return engine.enrich(df)


def _feature_columns(df: pl.DataFrame) -> list[str]:
    base = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
    return [c for c in df.columns if c not in base]


def _build_diff_rows(
    s_enriched: pl.DataFrame,
    p_enriched: pl.DataFrame,
    feature_cols: list[str],
    window_start: datetime,
) -> list[dict]:
    rows = []
    for i in range(s_enriched.height):
        ts = ensure_utc(s_enriched["timestamp"][i])
        if ts < window_start:
            continue
        s_row = s_enriched.row(i, named=True)
        p_row = p_enriched.row(i, named=True)
        rows.append(_diff_row(ts, s_row, p_row, feature_cols))
    return rows


def _summarize(rows: list[dict], feature_cols: list[str], pct_tol: float):
    price_divergent = 0
    volume_divergent = 0
    feature_divergent = 0
    max_price_pct = 0.0
    max_close_pct = 0.0
    max_volume_pct = 0.0
    worst_feature = ("", 0.0)

    for row in rows:
        close_pct = row.get("close_pct") or 0.0
        vol_pct = row.get("volume_pct") or 0.0
        max_price_pct = max(max_price_pct, *(row.get(f"{c}_pct") or 0.0 for c in PRICE_COLUMNS))
        max_close_pct = max(max_close_pct, close_pct)
        max_volume_pct = max(max_volume_pct, vol_pct)
        if any((row.get(f"{c}_pct") or 0.0) > pct_tol for c in PRICE_COLUMNS):
            price_divergent += 1
        if vol_pct > pct_tol:
            volume_divergent += 1

        feat_diverged = False
        for col in feature_cols:
            pct = row.get(f"{col}_pct")
            if pct is None:
                feat_diverged = True
                continue
            if pct > pct_tol:
                feat_diverged = True
                if pct > worst_feature[1]:
                    worst_feature = (col, pct)
        if feat_diverged:
            feature_divergent += 1

    return {
        "price_divergent": price_divergent,
        "volume_divergent": volume_divergent,
        "feature_divergent": feature_divergent,
        "max_price_pct": round(max_price_pct, 6),
        "max_close_pct": round(max_close_pct, 6),
        "max_volume_pct": round(max_volume_pct, 6),
        "worst_feature_name": worst_feature[0],
        "worst_feature_pct": round(worst_feature[1], 6),
    }


def _write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return path
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


async def _run(
    *,
    symbol: str,
    trading_days: int,
    pct_tolerance: float,
    csv_path: str | None,
    active_plan: str | None,
    session: str,
    max_signal_details: int,
) -> None:
    now = datetime.now(UTC)
    window_start = trading_window_start(now, trading_days)
    warmup_start = trading_window_start(now, trading_days + 5)

    print(f"SYMBOL={symbol} WINDOW_START={window_start.date()} TRADING_DAYS={trading_days} SESSION={session}")
    print("Fetching bars from both providers...")
    schwab_bars, polygon_bars = await _fetch_bars(symbol, warmup_start, now)
    print(f"SCHWAB_BARS={len(schwab_bars)} POLYGON_BARS={len(polygon_bars)}")

    if not schwab_bars or not polygon_bars:
        print("ERROR: one or both providers returned no bars")
        return

    s_df = _frame_from_bars(schwab_bars)
    p_df = _frame_from_bars(polygon_bars)
    s_aligned, p_aligned, s_only, p_only = _align(s_df, p_df)
    print(f"ALIGNED={s_aligned.height} SCHWAB_ONLY={s_only} POLYGON_ONLY={p_only}")
    full_counts = _session_counts(s_aligned, window_start)
    print(
        "ALIGNED_WINDOW_SESSION_COUNTS "
        f"all={full_counts['all']} regular={full_counts['regular']} extended={full_counts['extended']}"
    )

    if s_aligned.height == 0:
        print("ERROR: no overlapping bars between providers")
        return

    s_scoped = _filter_session(s_aligned, session)
    p_scoped = _filter_session(p_aligned, session)
    if s_scoped.is_empty() or p_scoped.is_empty():
        print(f"ERROR: no overlapping bars remain after session filter: {session}")
        return

    print(f"SCOPED_ALIGNED={s_scoped.height} session={session}")
    print("Enriching Schwab bars...")
    s_enriched = _enrich(s_scoped)
    print("Enriching Polygon bars...")
    p_enriched = _enrich(p_scoped)

    feature_cols = _feature_columns(s_enriched)
    diff_rows = _build_diff_rows(s_enriched, p_enriched, feature_cols, window_start)

    summary = _summarize(diff_rows, feature_cols, pct_tolerance)
    print(f"BARS_IN_WINDOW={len(diff_rows)}")
    print(f"PRICE_DIVERGENT={summary['price_divergent']} (pct_tolerance={pct_tolerance})")
    print(f"VOLUME_DIVERGENT={summary['volume_divergent']} (pct_tolerance={pct_tolerance})")
    print(f"FEATURE_DIVERGENT={summary['feature_divergent']}")
    print(f"MAX_PRICE_PCT_DIFF={summary['max_price_pct']}")
    print(f"MAX_CLOSE_PCT_DIFF={summary['max_close_pct']}")
    print(f"MAX_VOLUME_PCT_DIFF={summary['max_volume_pct']}")
    if summary["worst_feature_name"]:
        print(f"WORST_FEATURE={summary['worst_feature_name']} PCT_DIFF={summary['worst_feature_pct']}")

    if active_plan:
        await _compare_signals(
            active_plan,
            symbol,
            s_enriched,
            p_enriched,
            window_start,
            max_signal_details=max_signal_details,
        )

    if csv_path:
        written = _write_csv(Path(csv_path), diff_rows)
        print(f"CSV_WRITTEN={written} ROWS={len(diff_rows)}")


async def _compare_signals(
    active_plan_path: str,
    symbol: str,
    s_enriched: pl.DataFrame,
    p_enriched: pl.DataFrame,
    window_start: datetime,
    max_signal_details: int = 20,
) -> None:
    from bhiksha.app.bootstrap import build_runtime
    from bhiksha.app.replay import ReplaySignalEvaluator
    from bhiksha.market_data.feature_service import FeatureService

    runtime = build_runtime(active_plan_path=active_plan_path)
    evaluator = ReplaySignalEvaluator(FeatureService(), runtime.strategy_registry)
    deployments = [d for d in runtime.enabled_deployments if d.symbol == symbol]

    if not deployments:
        print(f"SIGNAL_COMPARISON: no deployments match {symbol}")
        return

    s_frame = s_enriched.select(["symbol", "timestamp", "open", "high", "low", "close", "volume"])
    p_frame = p_enriched.select(["symbol", "timestamp", "open", "high", "low", "close", "volume"])

    start_idx = 0
    for i, ts in enumerate(s_enriched["timestamp"].to_list()):
        if ensure_utc(ts) >= window_start:
            start_idx = i
            break

    for deployment in deployments:
        s_frames = evaluator.prepare_enriched_frames(s_frame, [deployment])
        p_frames = evaluator.prepare_enriched_frames(p_frame, [deployment])
        s_dep = s_frames[deployment.deployment_id]
        p_dep = p_frames[deployment.deployment_id]

        s_decisions = evaluator.scan_entry_history_with_index_on_enriched(
            deployment, s_dep, start_at=start_idx, signals_only=False,
        )
        p_decisions = evaluator.scan_entry_history_with_index_on_enriched(
            deployment, p_dep, start_at=start_idx, signals_only=False,
        )

        s_signals = {idx for idx, d in s_decisions if d.signal}
        p_signals = {idx for idx, d in p_decisions if d.signal}
        schwab_only = s_signals - p_signals
        polygon_only = p_signals - s_signals
        both = s_signals & p_signals

        print(
            f"SIGNAL_COMPARISON deployment={deployment.deployment_id} "
            f"schwab_signals={len(s_signals)} polygon_signals={len(p_signals)} "
            f"both={len(both)} schwab_only={len(schwab_only)} polygon_only={len(polygon_only)}"
        )
        s_by_idx = dict(s_decisions)
        p_by_idx = dict(p_decisions)
        for idx in sorted(schwab_only)[:max_signal_details]:
            ts_utc = ensure_utc(s_dep["timestamp"][idx])
            ts = ts_utc.astimezone(CT)
            schwab_decision = s_by_idx[idx]
            polygon_decision = p_by_idx.get(idx)
            print(
                f"  SCHWAB_ONLY bar={idx} time_ct={ts.strftime('%I:%M %p')} "
                f"session={_session_label(ts_utc)} reasons={schwab_decision.reason} "
                f"features={_compact_features(schwab_decision.features)}"
            )
            if polygon_decision is not None:
                print(
                    f"    POLYGON_SAME_BAR signal={polygon_decision.signal} "
                    f"reasons={polygon_decision.reason} features={_compact_features(polygon_decision.features)}"
                )
        if len(schwab_only) > max_signal_details:
            print(f"  SCHWAB_ONLY_DETAIL_TRUNCATED remaining={len(schwab_only) - max_signal_details}")
        for idx in sorted(polygon_only)[:max_signal_details]:
            ts_utc = ensure_utc(p_dep["timestamp"][idx])
            ts = ts_utc.astimezone(CT)
            polygon_decision = p_by_idx[idx]
            schwab_decision = s_by_idx.get(idx)
            print(
                f"  POLYGON_ONLY bar={idx} time_ct={ts.strftime('%I:%M %p')} "
                f"session={_session_label(ts_utc)} reasons={polygon_decision.reason} "
                f"features={_compact_features(polygon_decision.features)}"
            )
            if schwab_decision is not None:
                print(
                    f"    SCHWAB_SAME_BAR signal={schwab_decision.signal} "
                    f"reasons={schwab_decision.reason} features={_compact_features(schwab_decision.features)}"
                )
        if len(polygon_only) > max_signal_details:
            print(f"  POLYGON_ONLY_DETAIL_TRUNCATED remaining={len(polygon_only) - max_signal_details}")


def _compact_features(features: dict) -> dict:
    keys = [
        "close",
        "volume",
        "volume_ma_20",
        "vpoc_4h",
        "vpoc_dist_pct",
        "directional_mass",
        "z_score",
        "velocity",
        "velocity_1m",
        "accel_1m",
        "jerk",
        "jerk_smooth_1m",
        "prev_jerk_smooth_1m",
    ]
    compact = {}
    for key in keys:
        if key not in features:
            continue
        value = features[key]
        compact[key] = round(value, 6) if isinstance(value, float) else value
    return compact


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Compare Schwab vs Polygon bars and Newton features for a symbol",
    )
    parser.add_argument("--symbol", required=True, help="Ticker symbol to compare")
    parser.add_argument("--trading-days", type=int, default=3, help="NYSE trading days to look back")
    parser.add_argument("--pct-tolerance", type=float, default=0.001, help="Pct diff threshold for divergence (0.001 = 0.1%%)")
    parser.add_argument(
        "--session",
        choices=SESSION_CHOICES,
        default="all",
        help="Which aligned bars to enrich and compare. Use regular to isolate market-hours parity.",
    )
    parser.add_argument("--active-plan", default=None, help="Path to active plan JSON for signal comparison")
    parser.add_argument("--csv", default=None, help="Output CSV path for bar-by-bar diffs")
    parser.add_argument("--max-signal-details", type=int, default=20, help="Maximum one-sided signal details to print per deployment")
    args = parser.parse_args(argv)

    asyncio.run(
        _run(
            symbol=args.symbol,
            trading_days=args.trading_days,
            pct_tolerance=args.pct_tolerance,
            csv_path=args.csv,
            active_plan=args.active_plan,
            session=args.session,
            max_signal_details=max(args.max_signal_details, 0),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
