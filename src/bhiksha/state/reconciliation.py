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
    stop_orders_by_symbol = _index_open_stop_orders(orders or [])
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
                source="broker_sync",
                stop_order_id=stop_orders_by_symbol.get(option_symbol),
            )
        )
    return tracked


def _parse_option_root(option_symbol: str) -> str:
    match = _OPTION_ROOT_RE.match(option_symbol)
    return match.group(1) if match else ""


def _index_open_stop_orders(orders: Iterable[dict]) -> dict[str, str]:
    indexed: dict[str, str] = {}
    for order in orders:
        instrument = order.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        if str(order.get("type", "")).upper() != "STOP":
            continue
        if str(order.get("side", "")).upper() != "SELL":
            continue
        status = str(order.get("status", "")).upper()
        if status not in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            continue
        symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        order_id = order.get("orderId")
        if symbol and order_id:
            indexed[symbol] = str(order_id)
    return indexed
