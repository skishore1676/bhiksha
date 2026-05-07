"""Shadow trade evidence aggregation for operator reports."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_shadow_evidence(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate paper-entry, mark, and exit events by deployment."""
    trades_by_deployment: dict[str, dict[str, dict[str, Any]]] = {}

    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        deployment_id = _maybe_str(payload.get("deployment_id"))
        trade_id = _maybe_str(payload.get("trade_id"))
        if not deployment_id or not trade_id:
            continue

        deployment_trades = trades_by_deployment.setdefault(deployment_id, {})
        trade = deployment_trades.setdefault(
            trade_id,
            {
                "trade_id": trade_id,
                "symbol": _maybe_str(payload.get("symbol")),
                "option_symbol": _maybe_str(payload.get("option_symbol")),
                "quantity": _maybe_int(payload.get("quantity")),
                "entry_price": _maybe_float(payload.get("entry_price")),
                "entry_timestamp": payload.get("entry_timestamp"),
                "mark_count": 0,
                "mfe_usd": 0.0,
                "mae_usd": 0.0,
                "mfe_stop_r": 0.0,
                "mae_stop_r": 0.0,
                "exit_reason": [],
            },
        )
        trade["symbol"] = trade["symbol"] or _maybe_str(payload.get("symbol"))
        trade["option_symbol"] = trade["option_symbol"] or _maybe_str(
            payload.get("option_symbol")
        )
        trade["quantity"] = trade["quantity"] or _maybe_int(payload.get("quantity"))
        if trade["entry_price"] is None:
            trade["entry_price"] = _maybe_float(payload.get("entry_price"))

        event_type = event.get("event_type")
        if event_type == "shadow_entry_assumed":
            trade["entry_timestamp"] = payload.get("entry_timestamp") or trade.get(
                "entry_timestamp"
            )
            trade["risk_reasons"] = list(payload.get("risk_reasons") or [])
        elif event_type == "shadow_mark":
            trade["mark_count"] = int(trade.get("mark_count") or 0) + 1
            mark_price = _maybe_float(payload.get("mark_price"))
            pnl = _maybe_float(payload.get("unrealized_pnl_usd"))
            stop_r = _maybe_float(payload.get("unrealized_stop_r"))
            trade["latest_mark_price"] = mark_price
            trade["latest_unrealized_pnl_usd"] = pnl
            trade["latest_unrealized_stop_r"] = stop_r
            trade["latest_mark_created_at"] = event.get("created_at")
            _apply_excursion(trade, pnl, stop_r)
        elif event_type == "shadow_exit_assumed":
            pnl = _maybe_float(payload.get("realized_pnl_usd"))
            stop_r = _maybe_float(payload.get("realized_stop_r"))
            trade["exit_price"] = _maybe_float(payload.get("exit_price"))
            trade["realized_pnl_usd"] = pnl
            trade["realized_stop_r"] = stop_r
            trade["exit_order_id"] = payload.get("exit_order_id")
            trade["exit_reason"] = list(payload.get("reason") or [])
            trade["exit_created_at"] = event.get("created_at")
            _apply_excursion(trade, pnl, stop_r)

    return {
        deployment_id: _deployment_shadow_summary(trades)
        for deployment_id, trades in sorted(trades_by_deployment.items())
    }


def _deployment_shadow_summary(trades: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trade_list = [_clean_trade(trade) for trade in trades.values()]
    entry_count = sum(1 for trade in trade_list if trade.get("entry_price") is not None)
    exit_count = sum(1 for trade in trade_list if trade.get("realized_pnl_usd") is not None)
    realized_values = [_maybe_float(trade.get("realized_pnl_usd")) for trade in trade_list]
    realized_values = [value for value in realized_values if value is not None]
    realized_r_values = [_maybe_float(trade.get("realized_stop_r")) for trade in trade_list]
    realized_r_values = [value for value in realized_r_values if value is not None]
    mfe_values = [_maybe_float(trade.get("mfe_usd")) or 0.0 for trade in trade_list]
    mae_values = [_maybe_float(trade.get("mae_usd")) or 0.0 for trade in trade_list]
    mfe_r_values = [_maybe_float(trade.get("mfe_stop_r")) or 0.0 for trade in trade_list]
    mae_r_values = [_maybe_float(trade.get("mae_stop_r")) or 0.0 for trade in trade_list]

    return {
        "entry_count": entry_count,
        "exit_count": exit_count,
        "open_count": max(entry_count - exit_count, 0),
        "mark_count": sum(int(trade.get("mark_count") or 0) for trade in trade_list),
        "realized_pnl_usd": _round_money(sum(realized_values)),
        "realized_stop_r_sum": _round_metric(sum(realized_r_values)),
        "average_realized_stop_r": (
            _round_metric(sum(realized_r_values) / len(realized_r_values))
            if realized_r_values
            else None
        ),
        "trade_level_mfe_usd": _round_money(sum(mfe_values)),
        "trade_level_mae_usd": _round_money(sum(mae_values)),
        "trade_level_mfe_stop_r": _round_metric(sum(mfe_r_values)),
        "trade_level_mae_stop_r": _round_metric(sum(mae_r_values)),
        "trades": trade_list,
    }


def _apply_excursion(trade: dict[str, Any], pnl: float | None, stop_r: float | None) -> None:
    if pnl is not None:
        trade["mfe_usd"] = max(_maybe_float(trade.get("mfe_usd")) or 0.0, pnl)
        trade["mae_usd"] = min(_maybe_float(trade.get("mae_usd")) or 0.0, pnl)
    if stop_r is not None:
        trade["mfe_stop_r"] = max(_maybe_float(trade.get("mfe_stop_r")) or 0.0, stop_r)
        trade["mae_stop_r"] = min(_maybe_float(trade.get("mae_stop_r")) or 0.0, stop_r)


def _clean_trade(trade: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(trade)
    for key in (
        "entry_price",
        "exit_price",
        "realized_pnl_usd",
        "mfe_usd",
        "mae_usd",
        "latest_mark_price",
        "latest_unrealized_pnl_usd",
    ):
        if cleaned.get(key) is not None:
            cleaned[key] = _round_money(float(cleaned[key]))
    for key in (
        "realized_stop_r",
        "mfe_stop_r",
        "mae_stop_r",
        "latest_unrealized_stop_r",
    ):
        if cleaned.get(key) is not None:
            cleaned[key] = _round_metric(float(cleaned[key]))
    return cleaned


def _maybe_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_money(value: float) -> float:
    return round(value, 2)


def _round_metric(value: float) -> float:
    return round(value, 4)
