"""Shadow recorder for the profile-aware exit evaluator.

The evaluator (:mod:`bhiksha.execution.profile_exit`) is a pure function. This
module is the thin SHADOW-FIRST integration seam: it runs the evaluator against a
live (or mocked) option quote and either

* **records** the decision as a ``profile_exit_shadow`` event WITHOUT touching the
  broker (the default, and the only behavior in non-live modes), or
* hands back the mapped :class:`~bhiksha.domain.models.ExitDecision` for the
  *existing* runtime dispatch path when — and only when —
  :func:`~bhiksha.execution.profile_exit.profile_exit_dispatch_allowed` returns
  ``True``.

This module deliberately performs no order placement of its own. When dispatch is
allowed it returns the ExitDecision so the caller routes it through
``PositionMonitor`` -> ``supervisor._handle_exit_locked`` (square_off) or the
supervisor's breakeven-promotion branch (stop_to_breakeven) — i.e. the FSM
actions Bhiksha already implements. It is wired live-READY but is NOT enabled in
the live runtime loop by this change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from bhiksha.execution.profile_exit import (
    ProfileExitDecision,
    ProfileExitFields,
    ProfileExitState,
    ProfileFsmAction,
    ProfileLadderRule,
    ProfileMarketView,
    evaluate_profile_exit,
    profile_decision_to_exit_decision,
    profile_exit_dispatch_allowed,
)

UTC = timezone.utc


class ProfileExitDispatchError(Exception):
    """An ARMED profile-exit dispatch (the routed exit/stop action) failed.

    Distinct from a benign shadow-RECORD failure: the dispatch may have already
    cancelled the protective stop before failing to place the close, so the
    position can be unprotected. The supervisor surfaces this as a
    ``protective_stop_failure`` runtime_issue and PROPAGATES it (never swallows it
    as a shadow error, never returns the stale position as "managed") so an armed
    dispatch failure behaves like a native exit failure: loud, not silent. Never
    raised with the operator flag OFF (the gate never opens, so nothing dispatches).
    """


class EventSink(Protocol):
    """Subset of the event repository the recorder needs (async append)."""

    async def append(self, event_type: str, payload: dict[str, Any]) -> Any: ...


@dataclass(slots=True, frozen=True)
class ProfileExitShadowOutcome:
    """Result of one shadow/live evaluation pass."""

    decision: ProfileExitDecision
    recorded: bool
    dispatched: bool
    # The mapped domain ExitDecision when dispatch is allowed; None in shadow.
    exit_decision: Any | None = None
    # Whether the fail-closed dispatch ALLOWLIST is open for this position this
    # tick (``profile_exit_dispatch_allowed`` returned True). This is the
    # AUTHORITY signal, distinct from ``dispatched``: the gate can be open on a
    # HOLD tick (where ``dispatched`` is False because the profile took no action)
    # yet the profile route is still the exit authority for the position. The
    # supervisor uses this to make the native exit path yield while the profile
    # route owns the position (see the authority handoff in supervisor.py).
    dispatch_allowed: bool = False


async def evaluate_and_record_profile_exit(
    *,
    event_sink: EventSink,
    fields: ProfileExitFields,
    deployment_id: str,
    symbol: str,
    option_symbol: str | None,
    entry_premium: float,
    quantity: int,
    market: ProfileMarketView,
    entry_time: datetime | None,
    state: ProfileExitState,
    live: bool,
    deployment_shadow_only: bool,
    position_source: str | None,
    runtime_mode: str | None = None,
    now: datetime | None = None,
    require_bar_time_for_eod: bool = False,
) -> ProfileExitShadowOutcome:
    """Evaluate the profile ladder and record it; dispatch only if gated-open.

    SHADOW-FIRST contract:
      * Always appends a ``profile_exit_shadow`` event (the decision is always
        recorded, even a HOLD, for auditability).
      * Returns a mapped ``ExitDecision`` for the existing dispatch path ONLY
        when :func:`profile_exit_dispatch_allowed` is True. Otherwise
        ``exit_decision`` is ``None`` and ``dispatched`` is ``False`` — nothing
        reaches the broker.
    """
    now = now or datetime.now(UTC)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=entry_premium,
        quantity=quantity,
        market=market,
        entry_time=entry_time,
        now=now,
        state=state,
        require_bar_time_for_eod=require_bar_time_for_eod,
    )

    dispatch_allowed = profile_exit_dispatch_allowed(
        live=live,
        deployment_shadow_only=deployment_shadow_only,
        position_source=position_source,
        runtime_mode=runtime_mode,
    )
    would_act = decision.exit or decision.fsm_action is ProfileFsmAction.STOP_TO_BREAKEVEN
    dispatched = bool(dispatch_allowed and would_act)

    await event_sink.append(
        "profile_exit_shadow",
        {
            "deployment_id": deployment_id,
            "symbol": symbol,
            "option_symbol": option_symbol,
            "timestamp": now.isoformat(),
            "profile_id": decision.profile_id,
            "rule": decision.rule.value,
            "fsm_action": decision.fsm_action.value,
            "reason": decision.reason,
            "exit": decision.exit,
            "exit_quantity": decision.exit_quantity,
            "target_stop_price": decision.target_stop_price,
            "cancel_protection_orders": decision.cancel_protection_orders,
            "entry_premium": entry_premium,
            "quantity": quantity,
            "current_premium": market.current_premium,
            "mode": "live_dispatch" if dispatched else "shadow_record",
            "dispatch_allowed": dispatch_allowed,
            "features": decision.features,
        },
    )

    exit_decision = None
    if dispatched:
        exit_decision = profile_decision_to_exit_decision(
            decision,
            deployment_id=deployment_id,
            symbol=symbol,
            timestamp=now,
        )

    return ProfileExitShadowOutcome(
        decision=decision,
        recorded=True,
        dispatched=dispatched,
        exit_decision=exit_decision,
        dispatch_allowed=dispatch_allowed,
    )


__all__ = [
    "EventSink",
    "ProfileExitShadowOutcome",
    "evaluate_and_record_profile_exit",
    # re-exports for convenience
    "ProfileExitFields",
    "ProfileExitState",
    "ProfileMarketView",
    "ProfileExitDecision",
    "ProfileLadderRule",
    "ProfileFsmAction",
]
