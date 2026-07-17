"""Canonical Public order-status semantics used by execution and reconciliation."""

from __future__ import annotations

from typing import Any


# Public's documented enum uses CANCELLED. CANCELED, SUBMITTED, and
# ACKNOWLEDGED are retained for older payloads and test adapters already seen by
# Bhiksha. REPLACED is intentionally excluded: without the replacement order's
# identity, releasing ownership of the old order would be unsafe.
PUBLIC_DEAD_ORDER_STATUSES = frozenset({"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"})
PUBLIC_WORKING_ORDER_STATUSES = frozenset(
    {
        "NEW",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "QUEUED_CANCELLED",
    }
)


def normalize_public_order_status(status: Any) -> str:
    return str(status or "").strip().upper()


def public_order_confirmed_dead_unfilled(status: Any, payload: dict[str, Any] | None) -> bool:
    """Return true only when an order cannot fill and reports no executed quantity."""
    if normalize_public_order_status(status or (payload or {}).get("status")) not in PUBLIC_DEAD_ORDER_STATUSES:
        return False
    raw_filled = (payload or {}).get("filledQuantity")
    if raw_filled is None:
        return True
    try:
        return float(raw_filled) == 0.0
    except (TypeError, ValueError):
        return False
