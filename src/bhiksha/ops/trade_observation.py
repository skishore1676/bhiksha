"""Conservative reporting classifications for entry/trade observations.

The runtime can persist an estimated ``trade_sessions`` row before the broker
confirms an entry fill.  Reporting must therefore not treat the row alone as
proof that a position existed.  This module only classifies a terminal no-fill
when a persisted event carries terminal order state plus explicit zero-fill
evidence (including Public's ``filledQuantity: null`` idiom), or the runtime's
persisted ``safe_to_close`` verdict derived from that same broker readback.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
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
        if kind == "signal_evaluation" and payload.get("deployment_id"):
            grouped[payload["deployment_id"]].append(payload)
            symbols.setdefault(payload["deployment_id"], str(payload.get("symbol") or ""))
        # These stages exclusively describe underlying feed IO. Broker quote,
        # order, reconciliation and generic exception receipts retain their
        # existing safety classification without implying a bar-data gap.
        if kind == "provider_backoff" or (kind == "runtime_issue" and payload.get("stage") in {"market_data_provider", "warm_start", "manual_intrabar"}):
            source_issues.append({"event_id": event.get("event_id"), "event_type": kind,
                "symbol": payload.get("symbol"), "stage": payload.get("stage"),
                "error": payload.get("error"), "observed_at": event.get("created_at")})
    schedule = _evaluation_schedule(events)
    rows = []
    for ident in sorted(enabled | grouped.keys()):
        evidence = grouped.get(ident, [])
        gaps = sum(evaluation_coverage(payload) == "incomplete" for payload in evidence)
        terminal_gaps = sum(evaluation_coverage(payload) == "incomplete" and
            bool(set(payload.get("reason") or []) & {"weekly_invalidated", "weekly_filled", "weekly_expired"})
            for payload in evidence)
        cadence = schedule["lanes"].get(ident, {
            "scheduled_evaluation_count": None, "observed_scheduled_evaluation_count": None,
            "intentional_inactive_count": None, "missing_evaluation_count": None,
            "missing_source_tick_count": None, "unknown_evaluation_count": None,
            "expected_source_tick_count": None, "expected_cadence_proved": False,
            "schedule_status": "historical_schedule_unknown"})
        feed_issues = sum(issue["symbol"] in {None, "ALL", symbols.get(ident)} for issue in source_issues)
        status = ("incomplete" if gaps - terminal_gaps or feed_issues or cadence.get("missing_evaluation_count")
            or cadence.get("missing_source_tick_count") else
            "complete" if cadence.get("expected_cadence_proved") else "partial" if evidence else "unknown")
        rows.append({"deployment_id": ident, "evaluation_count": len(evidence),
            "incomplete_evaluation_count": gaps, "terminal_diagnostic_count": terminal_gaps,
            "blocking_incomplete_evaluation_count": gaps - terminal_gaps,
            "source_issue_count": feed_issues, "status": status, **cadence})
    status = (
        "incomplete" if source_issues or any(row["status"] == "incomplete" for row in rows)
        else "complete" if rows and all(row["status"] == "complete" for row in rows)
        else "partial" if rows and all(row["evaluation_count"] for row in rows)
        else "unknown"
    )
    inventory_source = (
        "startup_config" if historical_inventory else
        "provided_deployments" if deployments is not None else "evaluation_receipts_only"
    )
    return {"status": status, "lanes": rows, "source_issues": source_issues,
            "expected_cadence_proved": bool(rows) and all(row.get("expected_cadence_proved", False) for row in rows),
            "inventory_source": inventory_source, "runtime_intervals": schedule["runtime_intervals"],
            "cadence_method": "calendar_closed_minutes_during_recorded_runtime; manual_intrabar_conditional_on_fresh_snapshot",
            "counting_contract": "signal_evaluation only; never subtract signal_decision positives"}



def _evaluation_schedule(events):
    """Match conditional dispatch receipts; missing source minutes have unknown lane eligibility.

    Old ledgers without ready/end and source receipts stay explicitly unknown.
    A restart or process loss never silently extends the prior runtime interval.
    """
    from bhiksha.strategy.weekly_chart import session
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    def stamp(value):
        try:
            t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return t.astimezone(UTC) if t.tzinfo else None
        except (ValueError, TypeError):
            return None
    ticks, observed, inactive = set(), set(), set()
    inactive_reasons = defaultdict(Counter)
    source_ticks = defaultdict(set)
    configs, intervals, open_intervals, startups = {}, [], [], []
    lane_expected = defaultdict(set)
    for event in events:
        p = event.get("payload") or {}
        kind = event.get("event_type")
        key = p.get("config_fingerprint")
        at = stamp(event.get("created_at"))
        if kind == "startup_config":
            configs[key] = p.get("deployments", [])
            startups.append({"key": key, "at": at, "payload": p, "ready": False})
        elif kind == "evaluation_runtime_ready" and key in configs and at:
            open_intervals.append({"key": key, "ready_at": at, "deployments": configs[key]})
            for startup in reversed(startups):
                if startup["key"] == key:
                    startup["ready"] = True
                    break
        elif kind == "evaluation_runtime_ended" and at:
            for i in range(len(open_intervals) - 1, -1, -1):
                if open_intervals[i]["key"] == key:
                    interval = open_intervals.pop(i)
                    interval["ended_at"] = at
                    intervals.append(interval)
                    break
        if kind == "evaluation_source_tick":
            source, tick = p.get("source"), p.get("source_tick")
            tick = stamp(tick).isoformat() if stamp(tick) else None
            if source and tick:
                source_ticks[(source, p.get("symbol"))].add(tick)
                for ident in p.get("deployment_ids", []):
                    ticks.add((ident, source, tick))
        elif kind in {"signal_evaluation", "evaluation_inactive"}:
            triple = (p.get("deployment_id"), p.get("evaluation_source") or p.get("source"),
                      stamp(p.get("source_tick")).isoformat() if stamp(p.get("source_tick")) else None)
            if all(triple):
                (observed if kind == "signal_evaluation" else inactive).add(triple)
                if kind == "evaluation_inactive":
                    inactive_reasons[triple[0]][p.get("reason", "unknown")] += 1
    missing_source = Counter()
    unknown_interval_lanes = set()
    interval_rows = []
    for startup in startups:
        if not startup["ready"] and startup["payload"].get("session", {}).get("max_bars") != 0:
            unknown_interval_lanes.update(d["deployment_id"] for d in startup["payload"].get("deployments", [])
                                          if d.get("enabled", True))
            interval_rows.append({"started_at": startup["at"].isoformat() if startup["at"] else None,
                                  "ready_at": None, "ended_at": None, "status": "historical_readiness_unknown"})
    for interval in [*intervals, *open_intervals]:
        ready, ended = interval["ready_at"], interval.get("ended_at")
        interval_rows.append({"ready_at": ready.isoformat(), "ended_at": ended.isoformat() if ended else None,
                              "status": "bounded" if ended else "unknown_end"})
        for d in interval["deployments"]:
            if not d.get("enabled", True):
                continue
            ident = d["deployment_id"]
            lane_expected[ident]  # Include holidays/zero-bar intervals explicitly.
            if not ended:
                unknown_interval_lanes.add(ident)
                continue
            day = ready.astimezone(et).date()
            while day <= ended.astimezone(et).date():
                bounds = session(day.isoformat())
                if bounds:
                    opening, closing = bounds
                    minute = opening
                    while minute < closing:
                        # Only bars opened after readiness and closed before end.
                        if ready <= minute and minute + timedelta(minutes=1) <= ended:
                            lane_expected[ident].add((ident, "closed_bar_1m", minute.isoformat()))
                        minute += timedelta(minutes=1)
                day += timedelta(days=1)
            missing_source[ident] = sum(tick not in source_ticks[(source, d.get("symbol"))]
                                       for _, source, tick in lane_expected[ident])
            if d.get("strategy", {}).get("key") in {"manual_trigger", "manual_breakout"}:
                # Polls wait for IO; a nominal poll interval proves no fixed tick grid.
                unknown_interval_lanes.add(ident)
    rows = {}
    ids = {t[0] for t in ticks} | set(lane_expected) | unknown_interval_lanes
    for ident in ids:
        expected = {t for t in ticks if t[0] == ident}
        seen = expected & observed
        skipped = (expected & inactive) - observed
        missing = expected - observed - inactive
        bounded = ident in lane_expected and ident not in unknown_interval_lanes
        rows[ident] = {
            "scheduled_evaluation_count": len(expected), "observed_scheduled_evaluation_count": len(seen),
            "intentional_inactive_count": len(skipped), "intentional_inactive_reasons": dict(inactive_reasons[ident]),
            "missing_evaluation_count": len(missing), "missing_source_tick_count": missing_source[ident] if bounded else None,
            "unknown_evaluation_count": missing_source[ident] if bounded else None,
            "expected_source_tick_count": len(lane_expected[ident]) if bounded else None,
            "expected_cadence_proved": bounded and missing_source[ident] == 0 and not missing,
            "schedule_status": "bounded" if bounded else "unknown_runtime_interval",
        }
    return {"lanes": rows, "runtime_intervals": interval_rows}

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
