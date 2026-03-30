"""Broker-state reconciliation helpers."""

from __future__ import annotations

import re
from typing import Iterable

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import normalize_option_symbol
from bhiksha.state.position_tracker import TrackedPosition


_OPTION_ROOT_RE = re.compile(r"^([A-Z]+)")


def reconcile_public_positions(
    positions: Iterable[dict],
    deployments: list[DeploymentManifest],
    *,
    orders: Iterable[dict] | None = None,
    known_trades: Iterable[TradeRecord] | None = None,
) -> list[TrackedPosition]:
    """Map Public portfolio positions into tracked deployment positions."""
    deployments_by_symbol: dict[str, list[DeploymentManifest]] = {}
    for deployment in deployments:
        deployments_by_symbol.setdefault(deployment.symbol, []).append(deployment)
    stop_orders_by_symbol, target_orders_by_symbol = _index_open_exit_orders(orders or [])
    known_trades = list(known_trades or [])
    trades_by_option_symbol: dict[str, list[TradeRecord]] = {}
    trades_by_order_id: dict[str, TradeRecord] = {}
    for trade in known_trades:
        if trade.option_symbol:
            trades_by_option_symbol.setdefault(trade.option_symbol, []).append(trade)
        for order_id in (trade.entry_order_id, trade.stop_order_id, trade.target_order_id):
            if order_id:
                trades_by_order_id[order_id] = trade
    tracked: list[TrackedPosition] = []

    for position in positions:
        instrument = position.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        option_symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        symbol = _parse_option_root(option_symbol)
        stop_order = stop_orders_by_symbol.get(option_symbol) or {}
        target_order = target_orders_by_symbol.get(option_symbol) or {}
        matched_trade = _resolve_trade(
            option_symbol,
            stop_order_id=stop_order.get("order_id"),
            target_order_id=target_order.get("order_id"),
            trades_by_option_symbol=trades_by_option_symbol,
            trades_by_order_id=trades_by_order_id,
        )
        deployment = None
        trade_id = None
        entry_price = _parse_entry_price(position)
        if matched_trade is not None:
            deployment = next((candidate for candidate in deployments if candidate.deployment_id == matched_trade.deployment_id), None)
            trade_id = matched_trade.trade_id
            entry_price = entry_price or matched_trade.entry_price
        else:
            symbol_deployments = deployments_by_symbol.get(symbol, [])
            if len(symbol_deployments) == 1:
                deployment = symbol_deployments[0]
            else:
                continue
        if deployment is None:
            continue
        quantity = int(float(position.get("quantity", "0") or 0))
        if quantity <= 0:
            continue
        tracked.append(
            TrackedPosition(
                symbol=symbol,
                deployment_id=deployment.deployment_id,
                trade_id=trade_id,
                option_symbol=option_symbol,
                quantity=quantity,
                entry_price=entry_price,
                source="broker_sync",
                order_id=matched_trade.entry_order_id if matched_trade is not None else None,
                stop_order_id=stop_order.get("order_id") or (matched_trade.stop_order_id if matched_trade is not None else None),
                stop_price=stop_order.get("price") or (matched_trade.stop_price if matched_trade is not None else None),
                target_order_id=target_order.get("order_id") or (matched_trade.target_order_id if matched_trade is not None else None),
                target_price=target_order.get("price") or (matched_trade.target_price if matched_trade is not None else None),
            )
        )
    return tracked


def _resolve_trade(
    option_symbol: str,
    *,
    stop_order_id: str | None,
    target_order_id: str | None,
    trades_by_option_symbol: dict[str, list[TradeRecord]],
    trades_by_order_id: dict[str, TradeRecord],
) -> TradeRecord | None:
    for order_id in (stop_order_id, target_order_id):
        if order_id and order_id in trades_by_order_id:
            return trades_by_order_id[order_id]
    option_trades = trades_by_option_symbol.get(option_symbol, [])
    if len(option_trades) == 1:
        return option_trades[0]
    return None


def _parse_option_root(option_symbol: str) -> str:
    match = _OPTION_ROOT_RE.match(option_symbol)
    return match.group(1) if match else ""


def _index_open_exit_orders(orders: Iterable[dict]) -> tuple[dict[str, dict[str, float | str]], dict[str, dict[str, float | str]]]:
    stop_indexed: dict[str, dict[str, float | str]] = {}
    target_indexed: dict[str, dict[str, float | str]] = {}
    for order in orders:
        instrument = order.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        if str(order.get("side", "")).upper() != "SELL":
            continue
        status = str(order.get("status", "")).upper()
        if status not in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            continue
        symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        order_id = order.get("orderId")
        if not symbol or not order_id:
            continue
        order_type = str(order.get("type", "")).upper()
        if order_type == "STOP":
            stop_indexed[symbol] = {
                "order_id": str(order_id),
                "price": _maybe_float(order.get("stopPrice")),
            }
        elif order_type == "LIMIT":
            target_indexed[symbol] = {
                "order_id": str(order_id),
                "price": _maybe_float(order.get("limitPrice")),
            }
    return stop_indexed, target_indexed


def _parse_entry_price(position: dict) -> float | None:
    return _maybe_float(((position.get("costBasis") or {}).get("unitCost")))


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
