"""Generate a local browser chart review for Bhiksha trades."""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.domain.models import Bar
from bhiksha.market_data.adapters.polygon import PolygonBarSource
from bhiksha.market_data.adapters.public import PublicBarSource
from bhiksha.tools.export_thinkorswim_study import ThinkorswimTrade, load_trades


STATIC_DIR = Path(__file__).resolve().parents[1] / "chart_review" / "static"
ET = ZoneInfo("America/New_York")
REGULAR_SESSION_START = time(9, 30)
REGULAR_SESSION_END = time(16, 0)


async def build_chart_review(
    *,
    db_path: str | Path,
    output_dir: str | Path,
    symbols: set[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    include_open: bool = True,
    candle_source: str = "polygon",
    include_extended_hours: bool = False,
) -> Path:
    trades = load_trades(
        db_path,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        include_open=include_open,
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _copy_static_assets(output)

    bundle = await _build_bundle(
        trades,
        trade_source=str(db_path),
        candle_source=candle_source,
        include_extended_hours=include_extended_hours,
    )
    data_path = output / "data.json"
    data_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    return data_path


async def _build_bundle(
    trades: list[ThinkorswimTrade],
    *,
    trade_source: str,
    candle_source: str,
    include_extended_hours: bool,
) -> dict[str, Any]:
    grouped: dict[str, list[ThinkorswimTrade]] = {}
    for trade in trades:
        grouped.setdefault(trade.symbol.upper(), []).append(trade)

    warnings: list[str] = []
    symbols: dict[str, Any] = {}
    for symbol, symbol_trades in sorted(grouped.items()):
        bars = await _fetch_bars(symbol, symbol_trades, candle_source=candle_source)
        if not include_extended_hours:
            bars = [bar for bar in bars if _is_regular_session_bar(bar)]
        if not bars:
            warnings.append(f"No candles returned for {symbol}; markers cannot be plotted.")
        symbols[symbol] = _symbol_payload(symbol_trades, bars)

    return {
        "metadata": {
            "generatedAt": datetime.now(UTC).isoformat(),
            "tradeSource": trade_source,
            "candleSource": (
                f"{candle_source} 1m underlying OHLCV"
                if include_extended_hours
                else f"{candle_source} 1m underlying OHLCV regular session"
            ),
            "tradeCount": len(trades),
            "symbols": sorted(symbols),
            "warnings": warnings,
        },
        "symbols": symbols,
    }


async def _fetch_bars(
    symbol: str,
    trades: list[ThinkorswimTrade],
    *,
    candle_source: str,
) -> list[Bar]:
    start, end = _bar_window(trades)
    if candle_source == "public":
        source = PublicBarSource()
        try:
            return await source.warm_start(symbol, start, end)
        finally:
            await source.close()
    if candle_source == "polygon":
        source = PolygonBarSource()
        return await source.warm_start(symbol, start, end)
    raise ValueError(f"Unsupported candle source: {candle_source}")


def _bar_window(trades: list[ThinkorswimTrade]) -> tuple[datetime, datetime]:
    timestamps: list[datetime] = []
    for trade in trades:
        timestamps.append(trade.entry_timestamp)
        if trade.exit_filled_at is not None:
            timestamps.append(trade.exit_filled_at)
    if not timestamps:
        now = datetime.now(UTC)
        return now - timedelta(days=1), now
    start = min(timestamps).astimezone(UTC) - timedelta(hours=2)
    end = max(timestamps).astimezone(UTC) + timedelta(hours=2)
    return start, end


def _symbol_payload(trades: list[ThinkorswimTrade], bars: list[Bar]) -> dict[str, Any]:
    candle_payload = [_bar_payload(bar) for bar in bars]
    bar_times = [int(bar.timestamp.timestamp()) for bar in bars]
    warnings: list[str] = []
    trade_payloads = []
    for trade in trades:
        payload = _trade_payload(trade, bars, bar_times)
        if payload["entryMarkerTime"] is None:
            warnings.append(f"Missing entry candle for {trade.symbol} trade {trade.trade_id[:8]}.")
        if trade.exit_filled_at is not None and payload["exitMarkerTime"] is None:
            warnings.append(f"Missing exit candle for {trade.symbol} trade {trade.trade_id[:8]}.")
        trade_payloads.append(payload)
    return {
        "candles": candle_payload,
        "trades": trade_payloads,
        "warnings": warnings,
    }


def _bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "time": int(bar.timestamp.timestamp()),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "iso": bar.timestamp.isoformat(),
    }


def _is_regular_session_bar(bar: Bar) -> bool:
    timestamp_et = bar.timestamp.astimezone(ET)
    session_time = timestamp_et.timetz().replace(tzinfo=None)
    return REGULAR_SESSION_START <= session_time <= REGULAR_SESSION_END


def _trade_payload(
    trade: ThinkorswimTrade,
    bars: list[Bar],
    bar_times: list[int],
) -> dict[str, Any]:
    entry_time = int(trade.entry_timestamp.timestamp())
    exit_time = int(trade.exit_filled_at.timestamp()) if trade.exit_filled_at is not None else None
    entry_bar = _nearest_bar(bars, bar_times, entry_time)
    exit_bar = _nearest_bar(bars, bar_times, exit_time) if exit_time is not None else None
    return {
        "tradeId": trade.trade_id,
        "deploymentId": trade.deployment_id,
        "symbol": trade.symbol,
        "direction": _direction_for_trade(trade),
        "contractType": _contract_type(trade.option_symbol),
        "entryAction": "BUY",
        "exitAction": "SELL" if trade.exit_filled_at is not None else None,
        "optionSymbol": trade.option_symbol,
        "quantity": trade.quantity,
        "status": trade.status,
        "entryIso": trade.entry_timestamp.isoformat(),
        "entryTime": entry_time,
        "entryMarkerTime": int(entry_bar.timestamp.timestamp()) if entry_bar is not None else None,
        "entryPrice": trade.entry_price,
        "underlyingEntryPrice": trade.underlying_entry_price,
        "exitIso": trade.exit_filled_at.isoformat() if trade.exit_filled_at is not None else None,
        "exitTime": exit_time,
        "exitMarkerTime": int(exit_bar.timestamp.timestamp()) if exit_bar is not None else None,
        "exitMode": trade.exit_mode,
        "exitPrice": trade.exit_price,
        "underlyingExitApprox": exit_bar.close if exit_bar is not None else None,
        "optionPnl": _premium_pnl(trade),
    }


def _nearest_bar(bars: list[Bar], bar_times: list[int], timestamp: int | None) -> Bar | None:
    if timestamp is None or not bars:
        return None
    index = bisect.bisect_left(bar_times, timestamp)
    candidates = []
    if index < len(bars):
        candidates.append(bars[index])
    if index > 0:
        candidates.append(bars[index - 1])
    if not candidates:
        return None
    nearest = min(candidates, key=lambda bar: abs(int(bar.timestamp.timestamp()) - timestamp))
    if abs(int(nearest.timestamp.timestamp()) - timestamp) > 90:
        return None
    return nearest


def _direction_for_trade(trade: ThinkorswimTrade) -> str:
    lowered = f"{trade.deployment_id} {trade.option_symbol or ''}".lower()
    if "_short" in lowered or "p00" in lowered:
        return "short"
    return "long"


def _contract_type(option_symbol: str | None) -> str | None:
    if option_symbol is None:
        return None
    match = re.search(r"\d{6}([CP])\d{8}$", option_symbol)
    if match is None:
        return None
    return "CALL" if match.group(1) == "C" else "PUT"


def _premium_pnl(trade: ThinkorswimTrade) -> float | None:
    if trade.entry_price is None or trade.exit_price is None:
        return None
    return round((trade.exit_price - trade.entry_price) * trade.quantity * 100, 2)


def _copy_static_assets(output_dir: Path) -> None:
    for asset in STATIC_DIR.iterdir():
        if asset.is_file():
            shutil.copy2(asset, output_dir / asset.name)


def _copy_remote_db(remote: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "source_bhiksha.db"
    subprocess.run(["scp", remote, str(target)], check=True)
    return target


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Bhiksha's local trade chart review app")
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--db", default=None, help="Local Bhiksha SQLite DB path")
    source.add_argument("--remote-db", default=None, help="Remote DB path, e.g. oldmac:/Users/sunny/Documents/bhiksha/bhiksha.db")
    parser.add_argument("--output-dir", default="artifacts/chart_review", help="Directory for the generated viewer")
    parser.add_argument("--symbol", action="append", default=[], help="Underlying symbol to include. Repeatable.")
    parser.add_argument("--start-date", default=None, help="Filter entry date in ET, YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Filter entry date in ET, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7, help="Filter to the last N calendar days in ET")
    parser.add_argument("--all", action="store_true", help="Include all persisted trades instead of the --days window")
    parser.add_argument("--closed-only", action="store_true", help="Only include closed trades")
    parser.add_argument("--include-extended-hours", action="store_true", help="Include premarket/after-hours candles")
    parser.add_argument("--candle-source", choices=("public", "polygon"), default="polygon")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    if args.remote_db:
        db_path = _copy_remote_db(args.remote_db, output_dir)
        trade_source = args.remote_db
    else:
        db_path = Path(args.db or "bhiksha.db")
        trade_source = str(db_path)

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if not args.all and args.days is not None:
        end_date = end_date or datetime.now().astimezone().date()
        start_date = max(start_date or date.min, end_date - timedelta(days=max(args.days - 1, 0)))

    symbols = {symbol.upper() for symbol in args.symbol} if args.symbol else None
    data_path = asyncio.run(
        build_chart_review(
            db_path=db_path,
            output_dir=output_dir,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            include_open=not args.closed_only,
            candle_source=args.candle_source,
            include_extended_hours=args.include_extended_hours,
        )
    )
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    payload["metadata"]["tradeSource"] = trade_source
    data_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"TRADES={payload['metadata']['tradeCount']}")
    print(f"SYMBOLS={','.join(payload['metadata']['symbols'])}")
    print(f"DATA={data_path}")
    print(f"OPEN={output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
