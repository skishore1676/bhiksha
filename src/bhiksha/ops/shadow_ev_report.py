"""Daily shadow-EV report for Bhiksha paper (shadow) trading lanes.

Lineage
-------
There used to be a daily "shadow-EV" readout for the Mala/Bhiksha shadow
campaign. Its expected-value core lived in ``mala_v2`` as
``src/research/bhiksha_signal_ev.py`` (rendered ``BHIKSHA_SIGNAL_EV_REPORT.md``)
and was published to Obsidian by ``scripts/publish_shadow_decision_brief.py``.
The whole thing was driven by an OpenClaw cron wrapper on oldmac
(``~/.openclaw/workspace/scripts/mala-shadow-daily.sh`` -> the disabled
``ai.openclaw.trading-systems-watch`` launchd label) that died in the May 2026
OpenClaw migration.

That old report was a heavy research-side artifact: it replayed signals against
Polygon, imported the Mala research stack, and published a long decision brief.
This module is the clean v2 the operator actually needs on his phone: a short,
Bhiksha-owned, current-schema readout of what the shadow lanes are earning, so
it is obvious which paper strategies are trending toward promotion.

What it measures
----------------
A "shadow lane" is a deployment whose trades enter with
``entry_order_id = 'SHADOW_ENTRY'`` (see
``bhiksha.risk.risk_manager.SHADOW_ENTRY_ORDER_ID`` and
``bhiksha.execution.supervisor``). Per lane it reports, over both a rolling
last-N window and a since-anchor window:

- realized P&L including banked partial-scale legs (``trade_partial_fills``),
- win rate, average win, average loss,
- a simple realized EV per trade (mean realized P&L),
- the exit-rule mix, and
- an improving / degrading / flat trend flag.

Honest labeling: these are PAPER MARKS, not broker fills. The report never
implies a shadow lane made or lost real money.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

SHADOW_ENTRY_ORDER_ID = "SHADOW_ENTRY"
OPTION_CONTRACT_MULTIPLIER = 100

# The profile-exit "since" anchor: 2026-07-02 is when profile-aware exits went
# live and the current shadow book started accumulating comparable outcomes.
DEFAULT_SINCE = "2026-07-02"
DEFAULT_RECENT_WINDOW = 10
# A lane needs at least this many closed trades before an improving/degrading
# trend flag is honest rather than noise.
DEFAULT_TREND_MIN_TRADES = 4
# Half-split mean-P&L moves smaller than this (in USD/trade) read as "flat".
TREND_FLAT_BAND_USD = 5.0


@dataclass(slots=True, frozen=True)
class ShadowEvReportWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path


def write_shadow_ev_report(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    since: str = DEFAULT_SINCE,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    now: datetime | None = None,
) -> ShadowEvReportWriteResult:
    report = build_shadow_ev_report(
        db_path, since=since, recent_window=recent_window, now=now
    )
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_date"]
    json_path = target_dir / f"shadow_ev_report_{stamp}.json"
    markdown_path = target_dir / f"shadow_ev_report_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_shadow_ev_report_markdown(report), encoding="utf-8")
    return ShadowEvReportWriteResult(report=report, json_path=json_path, markdown_path=markdown_path)


def build_shadow_ev_report(
    db_path: str | Path,
    *,
    since: str = DEFAULT_SINCE,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    now: datetime | None = None,
    trend_min_trades: int = DEFAULT_TREND_MIN_TRADES,
) -> dict[str, Any]:
    """Build the shadow-EV report structure from Bhiksha's SQLite runtime DB."""
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    path = Path(db_path)
    if not path.exists():
        return _empty_report(generated_at, since=since, recent_window=recent_window)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = _load_shadow_trades(conn)
        partials_by_trade = _load_partial_legs(conn)

    open_count = sum(1 for row in rows if not _is_closed(row))
    closed = [
        trade
        for trade in (_build_trade(row, partials_by_trade.get(row["trade_id"], [])) for row in rows)
        if trade is not None
    ]
    # Chronological by realization time so "rolling last-N" and the trend split
    # are well-defined.
    closed.sort(key=lambda trade: (trade["realized_at"] or "", trade["trade_id"]))

    since_book = [trade for trade in closed if (trade["realized_date"] or "") >= since]
    recent_book = closed[-recent_window:] if recent_window > 0 else list(closed)

    lanes = _build_lane_sections(
        closed,
        since=since,
        recent_window=recent_window,
        trend_min_trades=trend_min_trades,
    )

    return {
        "schema": "bhiksha.shadow_ev_report.v1",
        "generated_at": generated_at.isoformat(),
        "generated_date": generated_at.date().isoformat(),
        "db_path": str(path),
        "paper_marks": True,
        "since": since,
        "recent_window": recent_window,
        "lane_count_traded": len({trade["deployment_id"] for trade in closed}),
        "lane_count_active_since": len(lanes["active_since"]),
        "open_paper_positions": open_count,
        "book": {
            "since": {**_metrics([t["realized_pnl_usd"] for t in since_book]), "window": f"since {since}"},
            "recent": {
                **_metrics([t["realized_pnl_usd"] for t in recent_book]),
                "window": f"rolling {recent_window}",
            },
            "all_time": _metrics([t["realized_pnl_usd"] for t in closed]),
            "trend": _trend([t["realized_pnl_usd"] for t in closed], min_total=trend_min_trades),
            "exit_mix": _exit_mix(since_book or closed),
        },
        "lanes": lanes["rows"],
    }


# --- data loading ----------------------------------------------------------
def _load_shadow_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trade_sessions" not in tables:
        return []
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_sessions)").fetchall()}
    desired = [
        "trade_id",
        "deployment_id",
        "symbol",
        "option_symbol",
        "quantity",
        "entry_price",
        "entry_timestamp",
        "status",
        "entry_order_id",
        "exit_price",
        "exit_filled_quantity",
        "exit_filled_at",
        "exit_mode",
        "exit_rule",
    ]
    selected = [column for column in desired if column in columns]
    if "entry_order_id" not in selected:
        return []
    rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM trade_sessions WHERE entry_order_id = ?",
        (SHADOW_ENTRY_ORDER_ID,),
    ).fetchall()
    return list(rows)


def _load_partial_legs(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trade_partial_fills" not in tables:
        return {}
    legs: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT trade_id, closed_quantity, fill_price, exit_rule, origin FROM trade_partial_fills"
    ).fetchall():
        legs.setdefault(row["trade_id"], []).append(row)
    return legs


# --- per-trade P&L (the load-bearing math) ---------------------------------
def _build_trade(row: sqlite3.Row, partial_legs: list[sqlite3.Row]) -> dict[str, Any] | None:
    """Realize a closed shadow trade's paper P&L, INCLUDING banked partial legs.

    ``trade_sessions.quantity`` is overwritten to the residual on every partial
    bank (see ``supervisor._handle_partial_scale_locked``), so the durable
    per-leg economics live in ``trade_partial_fills``. Full realized P&L is the
    final residual leg plus every banked partial leg, each priced against the
    shared option entry price::

        final_leg  = (exit_price - entry_price) * exit_qty        * 100
        partial_i  = (fill_price - entry_price) * closed_quantity * 100

    Returns ``None`` for a trade that is not a realizable closed round-trip.
    """
    if not _is_closed(row):
        return None
    entry = _maybe_float(row["entry_price"]) if "entry_price" in row.keys() else None
    exit_price = _maybe_float(row["exit_price"]) if "exit_price" in row.keys() else None
    if entry is None or exit_price is None:
        return None
    exit_qty = _maybe_int(_get(row, "exit_filled_quantity")) or _maybe_int(_get(row, "quantity")) or 0

    final_leg = (exit_price - entry) * exit_qty * OPTION_CONTRACT_MULTIPLIER
    partial_pnl = 0.0
    partial_count = 0
    for leg in partial_legs:
        fill_price = _maybe_float(leg["fill_price"])
        closed_qty = _maybe_int(leg["closed_quantity"])
        if fill_price is None or not closed_qty:
            continue
        partial_pnl += (fill_price - entry) * closed_qty * OPTION_CONTRACT_MULTIPLIER
        partial_count += 1

    realized = round(final_leg + partial_pnl, 2)
    realized_at = _maybe_str(_get(row, "exit_filled_at")) or _maybe_str(_get(row, "entry_timestamp"))
    return {
        "trade_id": row["trade_id"],
        "deployment_id": _maybe_str(_get(row, "deployment_id")) or "unknown",
        "symbol": _maybe_str(_get(row, "symbol")),
        "realized_pnl_usd": realized,
        "realized_at": realized_at,
        "realized_date": _date_prefix(realized_at),
        "exit_label": _exit_label(row),
        "partial_legs": partial_count,
    }


# --- aggregation -----------------------------------------------------------
def _build_lane_sections(
    closed: list[dict[str, Any]],
    *,
    since: str,
    recent_window: int,
    trend_min_trades: int,
) -> dict[str, Any]:
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for trade in closed:
        by_lane.setdefault(trade["deployment_id"], []).append(trade)

    rows: list[dict[str, Any]] = []
    active_since: list[str] = []
    for deployment_id, trades in by_lane.items():
        since_trades = [t for t in trades if (t["realized_date"] or "") >= since]
        recent_trades = trades[-recent_window:] if recent_window > 0 else list(trades)
        if since_trades:
            active_since.append(deployment_id)
        rows.append(
            {
                "deployment_id": deployment_id,
                "short_label": _short_lane(deployment_id),
                "last_traded": trades[-1]["realized_date"],
                "since": _metrics([t["realized_pnl_usd"] for t in since_trades]),
                "recent": _metrics([t["realized_pnl_usd"] for t in recent_trades]),
                "all_time": _metrics([t["realized_pnl_usd"] for t in trades]),
                "trend": _trend([t["realized_pnl_usd"] for t in trades], min_total=trend_min_trades),
                "exit_mix": _exit_mix(since_trades or trades),
            }
        )

    # Worst since-window EV first: the lanes most in need of a look. Lanes with
    # no since-window trades sort last (they are dormant, not actionable).
    rows.sort(key=lambda lane: (lane["since"]["trades"] == 0, _sort_key(lane["since"]["total_pnl_usd"])))
    return {"rows": rows, "active_since": active_since}


def _metrics(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = round(sum(pnls), 2)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "total_pnl_usd": total,
        "ev_per_trade_usd": round(total / n, 2) if n else None,
        "avg_win_usd": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_usd": round(sum(losses) / len(losses), 2) if losses else None,
    }


def _trend(pnls: list[float], *, min_total: int) -> str:
    """Improving/degrading/flat from a chronological P&L series.

    Half-splits the series and compares the mean paper P&L per trade of the
    older half against the more recent half. Returns ``"n/a"`` until there are
    enough trades for the comparison to be meaningful.
    """
    n = len(pnls)
    if n < max(min_total, 2):
        return "n/a"
    mid = n // 2
    older = pnls[:mid]
    recent = pnls[mid:]
    if not older or not recent:
        return "n/a"
    delta = (sum(recent) / len(recent)) - (sum(older) / len(older))
    if delta > TREND_FLAT_BAND_USD:
        return "improving"
    if delta < -TREND_FLAT_BAND_USD:
        return "degrading"
    return "flat"


def _exit_mix(trades: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(t["exit_label"] for t in trades).items()))


def _exit_label(row: sqlite3.Row) -> str:
    exit_rule = _maybe_str(_get(row, "exit_rule"))
    if exit_rule:
        return f"profile:{exit_rule}"
    exit_mode = str(_get(row, "exit_mode") or "").lower()
    if exit_mode:
        return exit_mode
    # A paper close with no profile rule and no exit_mode is a native strategy
    # (thesis) paper exit by elimination.
    return "strategy_paper"


# --- rendering -------------------------------------------------------------
def render_shadow_ev_report_markdown(report: dict[str, Any]) -> str:
    since = report["since"]
    window = report["recent_window"]
    book = report["book"]
    lines: list[str] = [
        f"# Shadow-EV Report {report['generated_date']}",
        "",
        "_Paper marks, not broker fills. Shadow lanes are simulated; no real money moved._",
        "",
        "## Book",
        "",
        f"- lanes traded (all-time): `{report['lane_count_traded']}`, "
        f"active since {since}: `{report['lane_count_active_since']}`",
        f"- open paper positions (unrealized): `{report['open_paper_positions']}`",
        f"- since {since}: {_metrics_md(book['since'])}",
        f"- rolling {window}: {_metrics_md(book['recent'])}",
        f"- all-time: {_metrics_md(book['all_time'])}",
        f"- book trend: `{book['trend']}`",
        f"- exit mix (since {since}): {_exit_mix_md(book['exit_mix'])}",
        "",
        "## Lanes",
        "",
        f"Metrics below are the **since {since}** window per lane "
        "(EV = mean paper P&L per trade).",
        "",
        "| Lane | Trades | Win% | Total | Avg win | Avg loss | EV/trade | Trend | Exit mix |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for lane in report["lanes"]:
        metric = lane["since"] if lane["since"]["trades"] else lane["all_time"]
        scope_note = "" if lane["since"]["trades"] else " _(all-time; dormant since window)_"
        lines.append(
            "| `{label}`{note} | {trades} | {wr} | {total} | {aw} | {al} | {ev} | {trend} | {mix} |".format(
                label=lane["short_label"],
                note=scope_note,
                trades=metric["trades"],
                wr=_fmt_pct(metric["win_rate"]),
                total=_fmt_money(metric["total_pnl_usd"]),
                aw=_fmt_money(metric["avg_win_usd"]),
                al=_fmt_money(metric["avg_loss_usd"]),
                ev=_fmt_money(metric["ev_per_trade_usd"]),
                trend=lane["trend"],
                mix=_exit_mix_md(lane["exit_mix"]),
            )
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_shadow_ev_report_telegram(report: dict[str, Any]) -> str:
    since = report["since"]
    window = report["recent_window"]
    book = report["book"]
    since_m = book["since"]
    recent_m = book["recent"]
    header = f"Shadow-EV {report['generated_date']} (paper marks, not fills)"
    lines = [
        header,
        f"Book since {since}: {_metrics_tg(since_m)} {_arrow(book['trend'])}",
        f"Rolling {window}: {_metrics_tg(recent_m)}",
        f"Lanes: {report['lane_count_active_since']} active / {report['lane_count_traded']} traded"
        + (f", {report['open_paper_positions']} open" if report["open_paper_positions"] else ""),
        "",
    ]
    active = [lane for lane in report["lanes"] if lane["since"]["trades"]]
    if active:
        lines.append(f"Lanes since {since} (worst first):")
        for lane in active:
            metric = lane["since"]
            lines.append(
                "- {label} n{n} ev{ev} wr{wr} {arrow}".format(
                    label=_clip(lane["short_label"], 30),
                    n=metric["trades"],
                    ev=_fmt_money_tag(metric["ev_per_trade_usd"]),
                    wr=_fmt_pct(metric["win_rate"], compact=True),
                    arrow=_arrow(lane["trend"]),
                )
            )
    else:
        lines.append(f"No shadow trades since {since}.")
    return "\n".join(lines)


# --- formatting helpers ----------------------------------------------------
def _metrics_md(metric: dict[str, Any]) -> str:
    return (
        f"`{metric['trades']}` trades, "
        f"win `{_fmt_pct(metric['win_rate'])}`, "
        f"total `{_fmt_money(metric['total_pnl_usd'])}`, "
        f"EV/trade `{_fmt_money(metric['ev_per_trade_usd'])}`, "
        f"avg win `{_fmt_money(metric['avg_win_usd'])}` / "
        f"avg loss `{_fmt_money(metric['avg_loss_usd'])}`"
    )


def _metrics_tg(metric: dict[str, Any]) -> str:
    return (
        f"{metric['trades']}t {_fmt_money_tag(metric['total_pnl_usd'])} "
        f"wr{_fmt_pct(metric['win_rate'], compact=True)} "
        f"ev{_fmt_money_tag(metric['ev_per_trade_usd'])}"
    )


def _exit_mix_md(mix: dict[str, int]) -> str:
    if not mix:
        return "`-`"
    return ", ".join(f"`{label}`×{count}" for label, count in mix.items())


def _arrow(trend: str) -> str:
    return {"improving": "↑", "degrading": "↓", "flat": "→"}.get(trend, "·")


def _short_lane(deployment_id: str) -> str:
    cleaned = re.sub(r"^strategy_", "", deployment_id or "")
    cleaned = re.sub(r"_shadow(_row_\d+)?$", "", cleaned)
    return cleaned or (deployment_id or "unknown")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # Keep the tail (symbol/side live at the end of a deployment id).
    return "…" + text[-(limit - 1):]


def _fmt_money(value: Any) -> str:
    number = _maybe_float(value)
    if number is None:
        return "-"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    body = f"{magnitude:,.0f}" if magnitude >= 100 else f"{magnitude:.2f}"
    return f"{sign}${body}"


def _fmt_money_tag(value: Any) -> str:
    number = _maybe_float(value)
    if number is None:
        return "n/a"
    sign = "-" if number < 0 else "+"
    return f"{sign}${abs(number):,.0f}"


def _fmt_pct(value: Any, *, compact: bool = False) -> str:
    number = _maybe_float(value)
    if number is None:
        return "n/a"
    return f"{number * 100:.0f}%" if compact else f"{number * 100:.1f}%"


def _sort_key(value: Any) -> float:
    number = _maybe_float(value)
    return number if number is not None else 0.0


def _empty_report(generated_at: datetime, *, since: str, recent_window: int) -> dict[str, Any]:
    empty = _metrics([])
    return {
        "schema": "bhiksha.shadow_ev_report.v1",
        "generated_at": generated_at.isoformat(),
        "generated_date": generated_at.date().isoformat(),
        "db_path": None,
        "paper_marks": True,
        "since": since,
        "recent_window": recent_window,
        "lane_count_traded": 0,
        "lane_count_active_since": 0,
        "open_paper_positions": 0,
        "book": {
            "since": {**empty, "window": f"since {since}"},
            "recent": {**empty, "window": f"rolling {recent_window}"},
            "all_time": empty,
            "trend": "n/a",
            "exit_mix": {},
        },
        "lanes": [],
    }


def _is_closed(row: sqlite3.Row) -> bool:
    return str(_get(row, "status") or "").lower() == "closed"


def _date_prefix(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace(" ", "T")[:10]


def _get(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _maybe_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
