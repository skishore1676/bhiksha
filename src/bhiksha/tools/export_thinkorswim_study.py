"""Export Bhiksha trade sessions as thinkorswim chart studies."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


@dataclass(slots=True, frozen=True)
class ThinkorswimTrade:
    trade_id: str
    deployment_id: str
    symbol: str
    option_symbol: str | None
    quantity: int
    entry_price: float | None
    underlying_entry_price: float | None
    entry_timestamp: datetime
    status: str
    stop_price: float | None
    target_price: float | None
    exit_mode: str | None
    exit_price: float | None
    exit_filled_at: datetime | None


def load_trades(
    db_path: str | Path,
    *,
    symbols: set[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    include_open: bool = True,
) -> list[ThinkorswimTrade]:
    """Load persisted trades that have an entry timestamp."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    with sqlite3.connect(path) as conn:
        columns = _table_columns(conn, "trade_sessions")
        rows = conn.execute(
            """
            SELECT *
            FROM trade_sessions
            WHERE entry_timestamp IS NOT NULL
            ORDER BY entry_timestamp ASC, trade_id ASC
            """
        ).fetchall()

    trades = [_trade_from_row(columns, row) for row in rows]
    filtered: list[ThinkorswimTrade] = []
    for trade in trades:
        if symbols is not None and trade.symbol.upper() not in symbols:
            continue
        if not include_open and trade.status != "closed":
            continue
        trade_date = trade.entry_timestamp.astimezone(ET).date()
        if start_date is not None and trade_date < start_date:
            continue
        if end_date is not None and trade_date > end_date:
            continue
        filtered.append(trade)
    return filtered


def write_studies(
    trades: list[ThinkorswimTrade],
    output_dir: str | Path,
    *,
    prefix: str = "bhiksha_trades",
) -> list[Path]:
    """Write one upper-chart thinkScript study per underlying symbol."""
    grouped: dict[str, list[ThinkorswimTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.symbol.upper()].append(trade)

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for symbol in sorted(grouped):
        path = output / f"{prefix}_{symbol}.ts"
        path.write_text(render_study(symbol, grouped[symbol]), encoding="utf-8")
        written.append(path)
    return written


def render_study(symbol: str, trades: list[ThinkorswimTrade]) -> str:
    lines = [
        "# Bhiksha trade markers",
        f"# Generated for {symbol.upper()} from persisted trade_sessions.",
        "# Apply this study to a 1-minute chart for the most precise placement.",
        "declare upper;",
        "",
        "input showEntryBubbles = yes;",
        "input showExitBubbles = yes;",
        "input showTradeLines = yes;",
        "input showSummaryLabel = yes;",
        "",
        f'def symbolOk = GetSymbol() == "{symbol.upper()}";',
        f'AddLabel(showSummaryLabel, "Bhiksha {symbol.upper()} trades: {len(trades)}", Color.WHITE);',
        "",
    ]
    for index, trade in enumerate(trades, start=1):
        lines.extend(_render_trade(index, trade))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_trade(index: int, trade: ThinkorswimTrade) -> list[str]:
    entry_et = trade.entry_timestamp.astimezone(ET)
    entry_day = _tos_day(entry_et)
    entry_time = _tos_time(entry_et)
    direction = _direction_for_trade(trade)
    entry_color = "Color.GREEN" if direction == "long" else "Color.RED"
    entry_tip = "low" if direction == "long" else "high"
    entry_up = "no" if direction == "long" else "yes"
    trade_short = trade.trade_id[:8]

    lines = [
        f"# {index}. {trade.trade_id} {trade.deployment_id}",
        f"def entry{index} = symbolOk and GetYYYYMMDD() == {entry_day} and SecondsFromTime({entry_time}) == 0;",
        (
            f'AddChartBubble(showEntryBubbles and entry{index}, {entry_tip}, '
            f'"ENTRY {direction.upper()}\\n{_escape(trade.option_symbol or trade.symbol)} '
            f'x{trade.quantity}\\n{_price_text("opt", trade.entry_price)} '
            f'{_price_text("und", trade.underlying_entry_price)}\\n{trade_short}", '
            f"{entry_color}, {entry_up});"
        ),
    ]

    if trade.exit_filled_at is not None:
        exit_et = trade.exit_filled_at.astimezone(ET)
        exit_day = _tos_day(exit_et)
        exit_time = _tos_time(exit_et)
        pnl = _premium_pnl(trade)
        pnl_text = f"\\nP/L ${pnl:.0f}" if pnl is not None else ""
        exit_tip = "high" if direction == "long" else "low"
        exit_up = "yes" if direction == "long" else "no"
        lines.extend(
            [
                f"def exit{index} = symbolOk and GetYYYYMMDD() == {exit_day} and SecondsFromTime({exit_time}) == 0;",
                (
                    f'AddChartBubble(showExitBubbles and exit{index}, {exit_tip}, '
                    f'"EXIT {_escape(trade.exit_mode or trade.status)}\\n'
                    f'{_price_text("opt", trade.exit_price)}{pnl_text}\\n{trade_short}", '
                    f"Color.YELLOW, {exit_up});"
                ),
                f'AddVerticalLine(showTradeLines and entry{index}, "B {index}", {entry_color});',
                f'AddVerticalLine(showTradeLines and exit{index}, "X {index}", Color.YELLOW);',
            ]
        )
    else:
        lines.append(f'AddVerticalLine(showTradeLines and entry{index}, "B {index}", {entry_color});')

    return lines


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _trade_from_row(columns: list[str], row: tuple[object, ...]) -> ThinkorswimTrade:
    values = dict(zip(columns, row, strict=True))
    entry_timestamp = _parse_timestamp(values.get("entry_timestamp"))
    if entry_timestamp is None:
        raise ValueError("trade row is missing entry_timestamp")
    return ThinkorswimTrade(
        trade_id=str(values.get("trade_id") or ""),
        deployment_id=str(values.get("deployment_id") or ""),
        symbol=str(values.get("symbol") or "").upper(),
        option_symbol=_maybe_str(values.get("option_symbol")),
        quantity=int(values.get("quantity") or 0),
        entry_price=_maybe_float(values.get("entry_price")),
        underlying_entry_price=_maybe_float(values.get("underlying_entry_price")),
        entry_timestamp=entry_timestamp,
        status=str(values.get("status") or ""),
        stop_price=_maybe_float(values.get("stop_price")),
        target_price=_maybe_float(values.get("target_price")),
        exit_mode=_maybe_str(values.get("exit_mode")),
        exit_price=_maybe_float(values.get("exit_price")),
        exit_filled_at=_parse_timestamp(values.get("exit_filled_at")),
    )


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _maybe_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tos_day(timestamp: datetime) -> int:
    return int(timestamp.strftime("%Y%m%d"))


def _tos_time(timestamp: datetime) -> int:
    return timestamp.hour * 100 + timestamp.minute


def _direction_for_trade(trade: ThinkorswimTrade) -> str:
    lowered = f"{trade.deployment_id} {trade.option_symbol or ''}".lower()
    if "_short" in lowered or "p00" in lowered:
        return "short"
    if "_long" in lowered or "c00" in lowered:
        return "long"
    return "long"


def _price_text(label: str, value: float | None) -> str:
    if value is None:
        return f"{label}=n/a"
    return f"{label}={value:.2f}"


def _premium_pnl(trade: ThinkorswimTrade) -> float | None:
    if trade.entry_price is None or trade.exit_price is None:
        return None
    return (trade.exit_price - trade.entry_price) * trade.quantity * 100


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Bhiksha trades as thinkorswim studies")
    parser.add_argument("--db", default="bhiksha.db", help="Path to Bhiksha SQLite database")
    parser.add_argument("--output-dir", default="artifacts/thinkorswim", help="Directory for generated .ts files")
    parser.add_argument("--symbol", action="append", default=[], help="Underlying symbol to export. Repeatable.")
    parser.add_argument("--start-date", default=None, help="Filter entry date in ET, YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Filter entry date in ET, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=None, help="Filter to the last N calendar days in ET")
    parser.add_argument("--closed-only", action="store_true", help="Only include closed trades")
    parser.add_argument("--prefix", default="bhiksha_trades", help="Output filename prefix")
    args = parser.parse_args(argv)

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if args.days is not None:
        end_date = end_date or datetime.now(ET).date()
        start_date = max(start_date or date.min, end_date - timedelta(days=max(args.days - 1, 0)))

    symbols = {symbol.upper() for symbol in args.symbol} if args.symbol else None
    trades = load_trades(
        args.db,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        include_open=not args.closed_only,
    )
    written = write_studies(trades, args.output_dir, prefix=args.prefix)
    print(f"TRADES={len(trades)}")
    print(f"FILES={len(written)}")
    for path in written:
        print(f"WROTE={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
