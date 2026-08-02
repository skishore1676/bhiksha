"""Conservative reporting classifications for entry/trade observations.

The runtime can persist an estimated ``trade_sessions`` row before the broker
confirms an entry fill.  Reporting must therefore not treat the row alone as
proof that a position existed.  This module only classifies a terminal no-fill
when a persisted event carries terminal order state plus explicit zero-fill
evidence (including Public's ``filledQuantity: null`` idiom), or the runtime's
persisted ``safe_to_close`` verdict derived from that same broker readback.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


FILLED_CLOSED = "FILLED/CLOSED"
ENTRY_CANCELLED_UNFILLED = "ENTRY_CANCELLED_UNFILLED"
NO_SIGNAL = "NO_SIGNAL"
BLOCKED = "BLOCKED"
NO_FILL = "NO_FILL"
MISSING = "MISSING"

NON_TRADE_OUTCOMES = frozenset({ENTRY_CANCELLED_UNFILLED, NO_FILL})

_CANCELLED_STATUSES = frozenset({"CANCELED", "CANCELLED"})
_NO_FILL_STATUSES = frozenset({"REJECTED", "EXPIRED"})
_TERMINAL_NO_FILL_STATUSES = _CANCELLED_STATUSES | _NO_FILL_STATUSES
_TERMINAL_ENTRY_EVENT_TYPES = frozenset(
    {
        "entry_reconcile_released",
        "entry_reprice_blocked",
        "entry_reprice_cancel_after_timeout",
    }
)


def index_terminal_entry_observations(
    events: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the latest proved terminal no-fill observation per trade id."""

    indexed: dict[str, dict[str, Any]] = {}
    for event in events:
        observation = terminal_entry_observation(event)
        if observation is None:
            continue
        trade_id = str(observation.get("trade_id") or "")
        if trade_id:
            indexed[trade_id] = observation
    return indexed


def terminal_entry_observation(event: dict[str, Any]) -> dict[str, Any] | None:
    """Classify one terminal entry event without inferring absent fill truth."""

    event_type = str(event.get("event_type") or "")
    if event_type not in _TERMINAL_ENTRY_EVENT_TYPES:
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id:
        return None

    broker_payload = payload.get("payload") or {}
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    status = str(payload.get("status") or broker_payload.get("status") or "").upper()
    if status not in _TERMINAL_NO_FILL_STATUSES:
        return None
    if payload.get("fill_quantity_ambiguous") is True:
        return None

    fill_evidence = _fill_evidence(payload, broker_payload)
    if fill_evidence == "positive_or_invalid":
        return None
    explicit_zero_fill = fill_evidence == "zero"
    safe_to_close = payload.get("safe_to_close") is True
    if not explicit_zero_fill and not safe_to_close:
        return None

    outcome = (
        ENTRY_CANCELLED_UNFILLED
        if status in _CANCELLED_STATUSES
        else NO_FILL
    )
    return {
        "observation_outcome": outcome,
        "trade_id": trade_id,
        "deployment_id": payload.get("deployment_id"),
        "symbol": payload.get("symbol"),
        "order_id": payload.get("order_id") or payload.get("entry_order_id"),
        "order_status": status,
        "source_event_type": event_type,
        "source_event_id": event.get("event_id"),
        "observed_at": event.get("created_at"),
        "pnl_eligible": False,
    }


def classify_trade_observation(
    trade: dict[str, Any],
    terminal_by_trade: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Classify a persisted trade row using only positive evidence."""

    trade_id = str(trade.get("trade_id") or "")
    terminal = terminal_by_trade.get(trade_id)
    if terminal is not None:
        if _has_positive_fill_or_exit_evidence(trade):
            return {
                "observation_outcome": MISSING,
                "trade_id": trade_id,
                "deployment_id": trade.get("deployment_id"),
                "symbol": trade.get("symbol"),
                "pnl_eligible": False,
                "source_event_type": terminal.get("source_event_type"),
                "source_event_id": terminal.get("source_event_id"),
                "observed_at": trade.get("updated_at")
                or trade.get("exit_filled_at"),
                "missing_reason": (
                    "contradictory_terminal_zero_fill_and_filled_trade"
                ),
            }
        return {
            **terminal,
            "deployment_id": terminal.get("deployment_id") or trade.get("deployment_id"),
            "symbol": terminal.get("symbol") or trade.get("symbol"),
        }

    if _is_closed(trade):
        if trade.get("realized_pnl_usd") is not None:
            return {
                "observation_outcome": FILLED_CLOSED,
                "trade_id": trade_id,
                "deployment_id": trade.get("deployment_id"),
                "symbol": trade.get("symbol"),
                "pnl_eligible": True,
                "source_event_type": None,
                "source_event_id": None,
                "observed_at": trade.get("exit_filled_at"),
            }
        return {
            "observation_outcome": MISSING,
            "trade_id": trade_id,
            "deployment_id": trade.get("deployment_id"),
            "symbol": trade.get("symbol"),
            "pnl_eligible": False,
            "source_event_type": None,
            "source_event_id": None,
            "observed_at": trade.get("updated_at") or trade.get("entry_timestamp"),
            "missing_reason": "closed_trade_missing_confirmed_exit_fill_truth",
        }
    return None


def group_events_by_deployment_day(
    events: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group reporting events without assigning an outcome."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        deployment_id = str(payload.get("deployment_id") or "")
        day = str(event.get("created_at") or "").replace(" ", "T")[:10]
        if deployment_id and day:
            grouped[(deployment_id, day)].append(event)
    return dict(grouped)


def _fill_evidence(
    payload: dict[str, Any], broker_payload: dict[str, Any]
) -> str:
    for container, key in (
        (payload, "filled_quantity"),
        (broker_payload, "filledQuantity"),
    ):
        if key not in container:
            continue
        value = container.get(key)
        if value is None or value == "":
            return "zero"
        try:
            return "zero" if int(value) == 0 else "positive_or_invalid"
        except (TypeError, ValueError):
            return "positive_or_invalid"
    return "absent"


def _is_closed(trade: dict[str, Any]) -> bool:
    return str(trade.get("status") or "").lower() == "closed"


def _has_positive_fill_or_exit_evidence(trade: dict[str, Any]) -> bool:
    if trade.get("realized_pnl_usd") is not None:
        return True
    try:
        if int(trade.get("exit_filled_quantity") or 0) > 0:
            return True
    except (TypeError, ValueError):
        return True
    return bool(
        trade.get("exit_order_id")
        and trade.get("exit_price") is not None
        and trade.get("exit_filled_at")
        and str(trade.get("exit_order_status") or "").upper() == "FILLED"
    )
