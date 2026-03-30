"""Broker-state reconciliation helpers."""

from __future__ import annotations

import re
from typing import Iterable

from bhiksha.config.models import DeploymentManifest
from bhiksha.execution.order_manager import normalize_option_symbol
from bhiksha.state.position_tracker import TrackedPosition


_OPTION_ROOT_RE = re.compile(r"^([A-Z]+)")


def reconcile_public_positions(
    positions: Iterable[dict],
    deployments: list[DeploymentManifest],
    *,
    orders: Iterable[dict] | None = None,
) -> list[TrackedPosition]:
    """Map Public portfolio positions into tracked deployment positions."""
    deployments_by_symbol = {deployment.symbol: deployment for deployment in deployments}
    stop_orders_by_symbol, target_orders_by_symbol = _index_open_exit_orders(orders or [])
    tracked: list[TrackedPosition] = []

    for position in positions:
        instrument = position.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        option_symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        symbol = _parse_option_root(option_symbol)
        deployment = deployments_by_symbol.get(symbol)
        if deployment is None:
            continue
        quantity = int(float(position.get("quantity", "0") or 0))
        if quantity <= 0:
            continue
        tracked.append(
            TrackedPosition(
                symbol=symbol,
                deployment_id=deployment.deployment_id,
                option_symbol=option_symbol,
                quantity=quantity,
                entry_price=_parse_entry_price(position),
                source="broker_sync",
                stop_order_id=(stop_orders_by_symbol.get(option_symbol) or {}).get("order_id"),
                stop_price=(stop_orders_by_symbol.get(option_symbol) or {}).get("price"),
                target_order_id=(target_orders_by_symbol.get(option_symbol) or {}).get("order_id"),
                target_price=(target_orders_by_symbol.get(option_symbol) or {}).get("price"),
            )
        )
    return tracked


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
