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
EXCLUDED = "EXCLUDED"
INCOMPLETE_COVERAGE_REASONS = frozenset({"weekly_confirmation_data_gap", "weekly_underlying_stale"})

# Closed by the operator on 2026-10-05. Raw ledger economics remain untouched;
# this exact historical row is not evidence of a breakeven strategy result.
_CLOSED_OPERATOR_CASE = {
    "trade_id": "edb45772-3304-4623-ad91-b6ed1c8f4a59",
    "deployment_id": "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
    "symbol": "IWM", "option_symbol": "IWM260929C00285000",
    "entry_order_id": "98f8868b-438f-4c46-a509-75605218b52c",
    "entry_timestamp": "2026-09-25T13:54:52.525000+00:00",
}


def reporting_exclusion(trade: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_closed(trade) or any(str(trade.get(key) or "") != value for key, value in _CLOSED_OPERATOR_CASE.items()):
        return None
    return {
        "observation_outcome": EXCLUDED, "pnl_eligible": False,
        "realized_pnl_usd": None, "economics_status": "excluded",
        "operator_case_status": "closed",
        "exclusion_reason": "operator_closed_historical_iwm_2026_09_25_unattributable_economics",
        "trade_id": trade.get("trade_id"), "deployment_id": trade.get("deployment_id"),
        "symbol": trade.get("symbol"), "observed_at": trade.get("entry_timestamp"),
    }


def evaluation_coverage(payload: dict[str, Any]) -> str:
    reasons = payload.get("reason") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    if INCOMPLETE_COVERAGE_REASONS.intersection(reasons) or (payload.get("features") or {}).get("evaluation_coverage") == "incomplete":
        return "incomplete"
    return "observed"


def summarize_evaluation_coverage(events: Iterable[dict[str, Any]], deployments=None) -> dict[str, Any]:
    """Receipt coverage is partial unless an explicit expected cadence is proved."""
    events = list(events)
    enabled: set[str] = set()
    symbols: dict[str, str] = {}
    historical_inventory = False
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") == "startup_config" and isinstance(payload.get("deployments"), list):
            historical_inventory = True
            for row in payload["deployments"]:
                if isinstance(row, dict) and row.get("enabled", True) and row.get("deployment_id"):
                    ident = row["deployment_id"]
                    enabled.add(ident)
                    symbols[ident] = str(row.get("symbol") or "")
    if not historical_inventory:
        for deployment in deployments or []:
            if getattr(deployment, "enabled", True):
                enabled.add(deployment.deployment_id)
                symbols[deployment.deployment_id] = str(getattr(deployment, "symbol", ""))

    grouped = defaultdict(list)
    source_issues = []
    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("event_type")
        if kind in {"signal_evaluation", "signal_decision"} and payload.get("deployment_id"):
            grouped[payload["deployment_id"]].append(payload)
            symbols.setdefault(payload["deployment_id"], str(payload.get("symbol") or ""))
        # These stages exclusively describe underlying feed IO. Broker quote,
        # order, reconciliation and generic exception receipts retain their
        # existing safety classification without implying a bar-data gap.
        if kind == "provider_backoff" or (kind == "runtime_issue" and payload.get("stage") in {"market_data_provider", "warm_start", "manual_intrabar"}):
            source_issues.append({"event_id": event.get("event_id"), "event_type": kind,
                "symbol": payload.get("symbol"), "stage": payload.get("stage"),
                "error": payload.get("error"), "observed_at": event.get("created_at")})
    rows = []
    for ident in sorted(enabled | grouped.keys()):
        evidence = grouped.get(ident, [])
        gaps = sum(evaluation_coverage(payload) == "incomplete" for payload in evidence)
        feed_issues = sum(issue["symbol"] in {None, "ALL", symbols.get(ident)} for issue in source_issues)
        status = "incomplete" if gaps or feed_issues else "partial" if evidence else "unknown"
        rows.append({"deployment_id": ident, "evaluation_count": len(evidence),
            "incomplete_evaluation_count": gaps, "source_issue_count": feed_issues, "status": status})
    status = (
        "incomplete" if source_issues or any(row["status"] == "incomplete" for row in rows)
        else "partial" if rows and all(row["evaluation_count"] for row in rows)
        else "unknown"
    )
    inventory_source = (
        "startup_config" if historical_inventory else
        "provided_deployments" if deployments is not None else "evaluation_receipts_only"
    )
    return {"status": status, "lanes": rows, "source_issues": source_issues,
            "expected_cadence_proved": False, "inventory_source": inventory_source}


def economics_summary(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    trades = list(trades)
    excluded = [t for t in trades if t.get("economics_status") == "excluded"]
    missing = [t for t in trades if t.get("realized_pnl_usd") is None and t not in excluded]
    priced = [t for t in trades if t.get("realized_pnl_usd") is not None and t not in excluded]
    subtotal = round(sum(t["realized_pnl_usd"] for t in priced), 2)
    return {"excluded_count": len(excluded), "missing_pnl_count": len(missing),
            "eligible_closed": len(priced), "known_subtotal_pnl_usd": subtotal if priced else None,
            "total_pnl_usd": None if excluded or missing else subtotal,
            "economics_status": "excluded" if excluded and not priced and not missing else "incomplete" if excluded or missing else "complete"}

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

    exclusion = reporting_exclusion(trade)
    if exclusion is not None:
        return exclusion
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
