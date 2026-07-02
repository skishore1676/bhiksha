"""Date-scoped runtime reports for live and shadow trading sessions."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_OPTION_SYMBOL_RE = re.compile(r"^[A-Z]+\d{6}[CP](\d{8})$")
# Single-name equities that legitimately trade at index-like price levels.
# MU crossed $1T market cap in 2026-05 and trades ~$1,100+; the strike/underlying
# ratio check above this allowlist still catches genuine quote-scaling errors.
_HIGH_PRICE_SYMBOL_ALLOWLIST = {"SPY", "QQQ", "IWM", "SMH", "MU"}


@dataclass(slots=True, frozen=True)
class DailyReportWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path


def write_daily_report(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    trading_date: date | str | None = None,
) -> DailyReportWriteResult:
    report = build_daily_report(db_path, trading_date=trading_date)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    day = report["trading_date"]
    json_path = target_dir / f"trade_session_report_{day}.json"
    markdown_path = target_dir / f"trade_session_report_{day}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_daily_report_markdown(report), encoding="utf-8")
    return DailyReportWriteResult(report=report, json_path=json_path, markdown_path=markdown_path)


def build_daily_report(
    db_path: str | Path,
    *,
    trading_date: date | str | None = None,
) -> dict[str, Any]:
    day = _coerce_day(trading_date)
    path = Path(db_path)
    if not path.exists():
        return _empty_report(day)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        events = _load_day_events(conn, day)
        trades = _load_day_trades(conn, day)

    event_counts = Counter(event["event_type"] for event in events)
    provider_events = _provider_events(events)
    lifecycle_events = _lifecycle_events(events)
    trades = [_augment_trade(trade) for trade in trades]
    live_trades = [trade for trade in trades if trade["lane"] == "live"]
    shadow_trades = [trade for trade in trades if trade["lane"] == "shadow"]
    open_positions = [trade for trade in trades if _is_open_trade(trade)]
    live_open_positions = [trade for trade in open_positions if trade["lane"] == "live"]
    shadow_open_positions = [trade for trade in open_positions if trade["lane"] == "shadow"]
    open_position_summary = _open_position_summary(open_positions)
    data_quality_warnings = _data_quality_warnings(trades)
    code_version = None
    for event in events:
        if event["event_type"] == "startup_config":
            code_version = (event["payload"] or {}).get("code_version") or code_version
    runtime_issue_counts = dict(
        sorted(
            Counter(
                str((event["payload"] or {}).get("category") or "exception")
                for event in events
                if event["event_type"] == "runtime_issue"
            ).items()
        )
    )

    return {
        "trading_date": day.isoformat(),
        "db_path": str(path),
        "code_version": code_version,
        "total_events": len(events),
        "event_type_counts": dict(sorted(event_counts.items())),
        "provider_health": {
            "reconciliation": provider_events,
            "runtime_issue_counts": runtime_issue_counts,
        },
        "trade_summary": {
            "live_count": len(live_trades),
            "shadow_count": len(shadow_trades),
            "live_open_count": len(live_open_positions),
            "shadow_open_count": len(shadow_open_positions),
            "total_open_count": len(open_positions),
            "live_realized_pnl_usd": _round_money(sum(_maybe_float(trade.get("realized_pnl_usd")) or 0.0 for trade in live_trades)),
            "shadow_realized_pnl_usd": _round_money(sum(_maybe_float(trade.get("realized_pnl_usd")) or 0.0 for trade in shadow_trades)),
            "total_realized_pnl_usd": _round_money(sum(_maybe_float(trade.get("realized_pnl_usd")) or 0.0 for trade in trades)),
        },
        "open_position_summary": open_position_summary,
        "open_positions": open_positions,
        "trades": trades,
        "lifecycle": lifecycle_events,
        "data_quality_warnings": data_quality_warnings,
        "status": _report_status(
            provider_events=provider_events,
            data_quality_warnings=data_quality_warnings,
            runtime_issue_counts=runtime_issue_counts,
            open_positions=open_positions,
        ),
    }


def render_daily_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("trade_summary") or {}
    status = report.get("status") or {}
    provider = ((report.get("provider_health") or {}).get("reconciliation") or {})
    lines = [
        f"# Bhiksha Trade Session - {report.get('trading_date')}",
        "",
        f"- status: `{status.get('level', 'UNKNOWN')}`",
        f"- live trades: `{summary.get('live_count', 0)}`",
        f"- shadow trades: `{summary.get('shadow_count', 0)}`",
        f"- open live positions: `{summary.get('live_open_count', 0)}`",
        f"- open shadow positions: `{summary.get('shadow_open_count', 0)}`",
        f"- live realized P&L: `${summary.get('live_realized_pnl_usd', 0.0):.2f}`",
        f"- shadow realized P&L: `${summary.get('shadow_realized_pnl_usd', 0.0):.2f}`",
        f"- total realized P&L: `${summary.get('total_realized_pnl_usd', 0.0):.2f}`",
        f"- reconciliation warnings: `{provider.get('warning_count', 0)}`",
        f"- reconciliation degraded: `{provider.get('degraded_count', 0)}`",
        f"- reconciliation blocking: `{provider.get('blocking_count', 0)}`",
        f"- data-quality warnings: `{len(report.get('data_quality_warnings') or [])}`",
    ]
    open_summary = report.get("open_position_summary") or {}
    if any(
        open_summary.get(key, 0)
        for key in ("protected_count", "target_active_count", "unprotected_count", "exit_pending_count")
    ):
        lines.append(
            "- open protection: "
            f"`protected {open_summary.get('protected_count', 0)}, "
            f"target active {open_summary.get('target_active_count', 0)}, "
            f"unprotected {open_summary.get('unprotected_count', 0)}, "
            f"exit pending {open_summary.get('exit_pending_count', 0)}`"
        )
    if status.get("reason"):
        lines.append(f"- reason: `{status['reason']}`")
    code_version = report.get("code_version") or {}
    if code_version.get("git_commit"):
        dirty_suffix = " (dirty)" if code_version.get("git_dirty") else ""
        lines.append(f"- code: `{str(code_version['git_commit'])[:12]}{dirty_suffix}`")

    open_positions = report.get("open_positions") or []
    if open_positions:
        lines.extend(["", "## Open Positions", "", "| Lane | Symbol | Option | Qty | Status | Entry | Stop | Target | Protection |", "|---|---|---|---:|---|---:|---:|---:|---|"])
        for position in open_positions:
            lines.append(
                "| {lane} | {symbol} | {option} | {qty} | {status} | {entry} | {stop} | {target} | {protection} |".format(
                    lane=position.get("lane", ""),
                    symbol=position.get("symbol", ""),
                    option=position.get("option_symbol") or "",
                    qty=position.get("quantity") or 0,
                    status=position.get("status") or "",
                    entry=_fmt_money(position.get("entry_price")),
                    stop=_fmt_money(position.get("stop_price")),
                    target=_fmt_money(position.get("target_price")),
                    protection=position.get("protection_state") or "",
                )
            )

    trades = report.get("trades") or []
    if trades:
        lines.extend(["", "## Trades", "", "| Lane | Symbol | Option | Qty | Entry | Exit | P&L | Status |", "|---|---|---|---:|---:|---:|---:|---|"])
        for trade in trades:
            lines.append(
                "| {lane} | {symbol} | {option} | {qty} | {entry} | {exit} | {pnl} | {status} |".format(
                    lane=trade.get("lane", ""),
                    symbol=trade.get("symbol", ""),
                    option=trade.get("option_symbol") or "",
                    qty=trade.get("quantity") or 0,
                    entry=_fmt_money(trade.get("entry_price")),
                    exit=_fmt_money(trade.get("exit_price")),
                    pnl=_fmt_money(trade.get("realized_pnl_usd")),
                    status=trade.get("status") or "",
                )
            )

    lifecycle = report.get("lifecycle") or {}
    if lifecycle:
        lines.extend(["", "## Lifecycle"])
        for key, value in sorted(lifecycle.items()):
            lines.append(f"- `{key}`: `{value}`")

    warnings = report.get("data_quality_warnings") or []
    if warnings:
        lines.extend(["", "## Data Quality Warnings"])
        for warning in warnings:
            lines.append(f"- `{warning.get('symbol')}` `{warning.get('option_symbol')}`: {warning.get('message')}")

    runtime_issue_counts = ((report.get("provider_health") or {}).get("runtime_issue_counts") or {})
    if runtime_issue_counts:
        lines.extend(["", "## Runtime Issues"])
        for category, count in sorted(runtime_issue_counts.items()):
            lines.append(f"- `{category}`: `{count}`")

    return "\n".join(lines) + "\n"


def render_daily_report_telegram_summary(
    report: dict[str, Any],
    *,
    markdown_path: str | Path | None = None,
) -> str:
    summary = report.get("trade_summary") or {}
    status = report.get("status") or {}
    provider = ((report.get("provider_health") or {}).get("reconciliation") or {})
    warnings = report.get("data_quality_warnings") or []
    trades = report.get("trades") or []
    open_summary = report.get("open_position_summary") or {}
    open_positions = report.get("open_positions") or []
    lines = [
        f"Bhiksha Session Report - {report.get('trading_date')}",
        f"Status: {status.get('level', 'UNKNOWN')} ({status.get('reason', 'ok')})",
        (
            "Open: "
            f"live {summary.get('live_open_count', 0)}, "
            f"shadow {summary.get('shadow_open_count', 0)}, "
            f"protected {open_summary.get('protected_count', 0)}, "
            f"target active {open_summary.get('target_active_count', 0)}, "
            f"unprotected {open_summary.get('unprotected_count', 0)}, "
            f"exit pending {open_summary.get('exit_pending_count', 0)}"
        ),
        (
            "P&L: "
            f"live ${summary.get('live_realized_pnl_usd', 0.0):.2f} "
            f"({summary.get('live_count', 0)} trades), "
            f"shadow ${summary.get('shadow_realized_pnl_usd', 0.0):.2f} "
            f"({summary.get('shadow_count', 0)} trades)"
        ),
        (
            "Reconciliation: "
            f"warn {provider.get('warning_count', 0)}, "
            f"degraded {provider.get('degraded_count', 0)}, "
            f"blocking {provider.get('blocking_count', 0)}"
        ),
    ]
    if open_positions:
        lines.append("Open positions:")
        for position in open_positions[:3]:
            lines.append(
                "- {lane} {symbol} {option} qty {qty}: entry {entry}, stop {stop}, target {target}, {protection}".format(
                    lane=position.get("lane", ""),
                    symbol=position.get("symbol", ""),
                    option=position.get("option_symbol") or "",
                    qty=position.get("quantity") or 0,
                    entry=_fmt_money(position.get("entry_price")) or "?",
                    stop=_fmt_money(position.get("stop_price")) or "?",
                    target=_fmt_money(position.get("target_price")) or "?",
                    protection=position.get("protection_state") or "unknown",
                )
            )
        if len(open_positions) > 3:
            lines.append(f"- +{len(open_positions) - 3} more open")
    if warnings:
        first = warnings[0]
        more = len(warnings) - 1
        suffix = f" +{more} more" if more else ""
        lines.append(
            "Data quality: "
            f"{len(warnings)} warning(s); first={first.get('symbol')} "
            f"{first.get('message')}{suffix}"
        )
    closed_trades = [item for item in trades if not _is_open_trade(item)]
    if closed_trades:
        lines.append("Recent trades:")
        for trade in closed_trades[:3]:
            lines.append(
                "- {lane} {symbol} {option} qty {qty}: {entry}->{exit}, P&L ${pnl}".format(
                    lane=trade.get("lane", ""),
                    symbol=trade.get("symbol", ""),
                    option=trade.get("option_symbol") or "",
                    qty=trade.get("quantity") or 0,
                    entry=_fmt_money(trade.get("entry_price")) or "?",
                    exit=_fmt_money(trade.get("exit_price")) or "?",
                    pnl=_fmt_money(trade.get("realized_pnl_usd")) or "0.00",
                )
            )
        if len(closed_trades) > 3:
            lines.append(f"- +{len(closed_trades) - 3} more closed in report")
    if markdown_path is not None:
        lines.append(f"Report: {markdown_path}")
    return "\n".join(lines)


def _empty_report(day: date) -> dict[str, Any]:
    return {
        "trading_date": day.isoformat(),
        "db_path": "",
        "total_events": 0,
        "event_type_counts": {},
        "provider_health": {"reconciliation": _empty_reconciliation(), "runtime_issue_counts": {}},
        "trade_summary": {
            "live_count": 0,
            "shadow_count": 0,
            "live_realized_pnl_usd": 0.0,
            "shadow_realized_pnl_usd": 0.0,
            "total_realized_pnl_usd": 0.0,
            "live_open_count": 0,
            "shadow_open_count": 0,
            "total_open_count": 0,
        },
        "open_position_summary": _open_position_summary([]),
        "open_positions": [],
        "trades": [],
        "lifecycle": {},
        "data_quality_warnings": [],
        "status": {"level": "NO_DATA", "reason": "db_missing"},
    }


def _load_day_events(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "events" not in tables:
        return []
    start = f"{day.isoformat()}T00:00:00"
    end = f"{day.isoformat()}T99:99:99"
    rows = conn.execute(
        "SELECT created_at, event_type, payload FROM events WHERE created_at >= ? AND created_at <= ? ORDER BY id",
        (start, end),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = _safe_json(row["payload"])
        events.append({"created_at": row["created_at"], "event_type": row["event_type"], "payload": payload})
    return events


def _load_day_trades(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
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
        "underlying_entry_price",
        "entry_timestamp",
        "status",
        "entry_order_id",
        "stop_order_id",
        "stop_price",
        "target_order_id",
        "target_price",
        "exit_order_id",
        "exit_price",
        "exit_filled_quantity",
        "exit_filled_at",
        "exit_order_status",
        "exit_order_type",
    ]
    selected = [column for column in desired if column in columns]
    day_text = day.isoformat()
    predicates: list[str] = []
    params: list[str] = []
    if "entry_timestamp" in columns:
        predicates.append("substr(replace(COALESCE(entry_timestamp, ''), ' ', 'T'), 1, 10) = ?")
        params.append(day_text)
    if "exit_filled_at" in columns:
        predicates.append("substr(replace(COALESCE(exit_filled_at, ''), ' ', 'T'), 1, 10) = ?")
        params.append(day_text)
    if "status" in columns:
        predicates.append("status != 'closed'")
    if not predicates:
        return []
    order_columns = [column for column in ("entry_timestamp", "exit_filled_at", "trade_id") if column in columns]
    order_expr = f"COALESCE({', '.join(order_columns)})" if len(order_columns) > 1 else order_columns[0]
    rows = conn.execute(
        f"""
        SELECT {", ".join(selected)}
        FROM trade_sessions
        WHERE {" OR ".join(predicates)}
        ORDER BY {order_expr}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _augment_trade(trade: dict[str, Any]) -> dict[str, Any]:
    entry = _maybe_float(trade.get("entry_price"))
    exit_price = _maybe_float(trade.get("exit_price"))
    quantity = _maybe_int(trade.get("exit_filled_quantity")) or _maybe_int(trade.get("quantity")) or 0
    realized = None
    if entry is not None and exit_price is not None and quantity:
        realized = _round_money((exit_price - entry) * quantity * 100)
    lane = "shadow" if _is_shadow_trade(trade) else "live"
    return {
        **trade,
        "lane": lane,
        "realized_pnl_usd": realized,
        "option_strike": _parse_option_strike(_maybe_str(trade.get("option_symbol"))),
        "protection_state": _protection_state(trade),
    }


def _provider_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    reconciliation_events = [
        event for event in events if event["event_type"] in {"reconciliation_health", "runtime_issue"}
        and (
            event["event_type"] == "reconciliation_health"
            or (event.get("payload") or {}).get("stage") == "reconciliation"
        )
    ]
    severity_counts = Counter(str((event["payload"] or {}).get("severity") or "runtime_issue") for event in reconciliation_events)
    return {
        **_empty_reconciliation(),
        "warning_count": severity_counts.get("warning", 0),
        "degraded_count": severity_counts.get("degraded", 0),
        "blocking_count": severity_counts.get("blocking", 0),
        "runtime_issue_count": severity_counts.get("runtime_issue", 0),
        "events": [
            {
                "created_at": event["created_at"],
                "event_type": event["event_type"],
                "severity": (event["payload"] or {}).get("severity"),
                "reason": (event["payload"] or {}).get("reason"),
                "error": (event["payload"] or {}).get("error"),
            }
            for event in reconciliation_events[-10:]
        ],
    }


def _empty_reconciliation() -> dict[str, Any]:
    return {"warning_count": 0, "degraded_count": 0, "blocking_count": 0, "runtime_issue_count": 0, "events": []}


def _lifecycle_events(events: list[dict[str, Any]]) -> dict[str, int]:
    interesting = {
        "target_approach_detected",
        "virtual_target_activation",
        "virtual_target_pullback_restore",
        "profit_target_armed",
        "protective_stop_submission",
        "protection_restore_attempt",
        "hard_flat_submission",
        "shadow_exit_assumed",
        "exit_plan",
        "exit_decision",
    }
    return dict(sorted(Counter(event["event_type"] for event in events if event["event_type"] in interesting).items()))


def _data_quality_warnings(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for trade in trades:
        underlying = _maybe_float(trade.get("underlying_entry_price"))
        strike = _maybe_float(trade.get("option_strike"))
        if underlying is None or strike is None or strike <= 0:
            continue
        ratio = underlying / strike
        if ratio < 0.5 or ratio > 2.0:
            warnings.append(
                {
                    "trade_id": trade.get("trade_id"),
                    "deployment_id": trade.get("deployment_id"),
                    "symbol": trade.get("symbol"),
                    "option_symbol": trade.get("option_symbol"),
                    "underlying_entry_price": underlying,
                    "option_strike": strike,
                    "ratio": round(ratio, 4),
                    "message": "underlying entry price is far from option strike; check quote scaling before using this as promotion evidence",
                }
            )
            continue
        symbol = str(trade.get("symbol") or "")
        if symbol not in _HIGH_PRICE_SYMBOL_ALLOWLIST and underlying >= 500 and strike >= 500:
            warnings.append(
                {
                    "trade_id": trade.get("trade_id"),
                    "deployment_id": trade.get("deployment_id"),
                    "symbol": symbol,
                    "option_symbol": trade.get("option_symbol"),
                    "underlying_entry_price": underlying,
                    "option_strike": strike,
                    "ratio": round(ratio, 4),
                    "message": "single-name equity has index-like underlying and strike levels; check quote scaling before using this as promotion evidence",
                }
            )
    return warnings


def _is_open_trade(trade: dict[str, Any]) -> bool:
    return str(trade.get("status") or "").lower() != "closed"


def _protection_state(trade: dict[str, Any]) -> str:
    status = str(trade.get("status") or "").lower()
    if status == "closed":
        return "closed"
    if trade.get("exit_order_id") or status.endswith("exit_pending") or "exit_pending" in status:
        return "exit_pending"
    if status == "target_active":
        return "target_active"
    if trade.get("stop_order_id"):
        return "protected"
    return "unprotected"


def _open_position_summary(open_positions: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_protection_state(position) for position in open_positions)
    return {
        "protected_count": counts.get("protected", 0),
        "target_active_count": counts.get("target_active", 0),
        "unprotected_count": counts.get("unprotected", 0),
        "exit_pending_count": counts.get("exit_pending", 0),
    }


def _report_status(
    *,
    provider_events: dict[str, Any],
    data_quality_warnings: list[dict[str, Any]],
    runtime_issue_counts: dict[str, int] | None = None,
    open_positions: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    if any(
        str(position.get("lane") or "").lower() == "live"
        and position.get("protection_state") == "unprotected"
        for position in (open_positions or [])
    ):
        return {"level": "RED", "reason": "live_open_unprotected"}
    if (runtime_issue_counts or {}).get("dead_lane", 0) > 0:
        return {"level": "RED", "reason": "dead_live_lane"}
    if provider_events.get("blocking_count", 0) > 0:
        return {"level": "RED", "reason": "blocking_reconciliation_failure"}
    if provider_events.get("degraded_count", 0) > 0:
        return {"level": "YELLOW", "reason": "degraded_reconciliation"}
    if data_quality_warnings:
        return {"level": "YELLOW", "reason": "data_quality_warning"}
    if provider_events.get("warning_count", 0) > 0 or provider_events.get("runtime_issue_count", 0) > 0:
        return {"level": "YELLOW", "reason": "provider_warning"}
    return {"level": "GREEN", "reason": "ok"}


def _is_shadow_trade(trade: dict[str, Any]) -> bool:
    deployment_id = str(trade.get("deployment_id") or "").lower()
    entry_order_id = str(trade.get("entry_order_id") or "")
    exit_order_id = str(trade.get("exit_order_id") or "")
    exit_order_type = str(trade.get("exit_order_type") or "").upper()
    return (
        "shadow" in deployment_id
        or entry_order_id.startswith("SHADOW")
        or exit_order_id.startswith("DRY_RUN")
        or exit_order_type == "PAPER"
    )


def _parse_option_strike(option_symbol: str | None) -> float | None:
    if not option_symbol:
        return None
    match = _OPTION_SYMBOL_RE.match(option_symbol)
    if not match:
        return None
    return int(match.group(1)) / 1000.0


def _safe_json(payload_text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(payload_text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_day(value: date | str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


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


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _fmt_money(value: Any) -> str:
    number = _maybe_float(value)
    return "" if number is None else f"{number:.2f}"
