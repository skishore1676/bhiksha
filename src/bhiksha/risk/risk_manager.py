"""Bhiksha risk manager: the two mechanical rails ordered after the 8-day,
$1,030 live loss that nothing mechanical stopped.

This module is the single bounded decision core for both rails. It is
deliberately NOT a framework: no new queue, no dashboard, no scheduler. It is
consulted synchronously from two existing seams (mirroring the shape of
``bhiksha.risk.cash_guard.CashGuard``, the repo's existing risk-gate
precedent):

  * ``allow_entry(deployment)`` -- an entry-planning consult point. Returns a
    block reason string (or ``None``) the SAME way
    ``BhikshaRuntime._reconciliation_live_entry_block_reason`` already does,
    so it plugs into ``ExecutionSupervisor.handle_signal``'s existing
    ``live_entry_block_reason`` parameter without changing that method's
    contract.
  * ``book_actions()`` -- a periodic/tick consult point that computes the
    day's realized live P&L and returns whether Rail A tier 2 (flatten) has
    been breached. The caller (``BhikshaRuntime``) is responsible for
    actually invoking the EXISTING ``supervisor.halt_and_flatten_positions``
    machinery when told to -- this module does not place or cancel orders.
  * ``reserve_sized_entry(...)`` -- the final live-only consult after option
    quote, quantity, and preflight are known. It durably reserves planned-stop
    loss headroom and enforces the confirmed correlation-cluster cap before a
    broker submission can occur.

RAIL A (two-tier portfolio daily-drawdown cap, realized-only v1):
  - tier 1 (halt): today's realized LIVE P&L <= -(max_daily_drawdown_pct/100)
    * usable_budget -> block all new entries this session.
  - tier 2 (flatten): <= -(flatten_daily_drawdown_pct/100) * usable_budget ->
    ALSO flatten open live positions via the caller's existing
    halt_and_flatten_positions/EmergencyBiasControl seam.
  - Missing data is fail-safe for EXISTING positions / flatten, not fail-open
    on a computed breach: no cash_budget_days row for today -> INACTIVE + one
    warning event, never a spurious halt or flatten. A P&L query failure is
    the same: INACTIVE + warning. ``book_actions()`` (the flatten path) never
    acts on missing data -- once both budget and P&L are available, a
    computed breach always acts, there is no "fail open" flatten path once
    data exists.
  - Missing/unknown budget is FAIL-CLOSED for NEW entries specifically
    (operator audit P2, 2026-07-03 finding): ``allow_entry`` returns
    ``allowed=False, reason="risk_rail_a_budget_unavailable"`` whenever Rail A
    is enabled but the budget read came back ``no_cash_budget_day`` or
    ``cash_budget_query_failed`` -- unknown budget must not authorize new
    live risk, even though it must not flatten existing risk either. See
    ``BhikshaRuntime.run_session``'s startup budget prefetch
    (``bhiksha.app.runtime.prefetch_cash_budget_day``), which upserts the
    row before the bar loop starts specifically to keep this window short.

RAIL B (per-deployment auto-demote, operator-resettable):
  - over the rolling last N (demote_window, default 10) CLOSED LIVE trades
    with complete realized economics for a deployment, once at least min_n
    (default 10) priced trades exist, if mean P&L per trade <
    demote_threshold_usd (default $0) -> demote: block
    further live entries for it this session (consult-point decision) AND
    persist a local override (``DemotionStore``) the active-plan compiler
    merges at next compile to force the row to shadow.
  - never auto-re-promotes. A protected operator re-promotion records a cutoff
    and Rail B requires a fresh post-cutoff window before it can demote again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bhiksha.domain.models import EntryRiskReservation, PartialFillRecord, TradeRecord
from bhiksha.ops.alerts import AlertMode, send_lathi_alert
from bhiksha.persistence.repository import CashBudgetRepository, EventRepository, TradeStateRepository
from bhiksha.risk.canary_inhibition_store import (
    CanaryInhibitionStore,
    CanaryInhibitionStoreError,
)
from bhiksha.risk.clusters import correlation_cluster
from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.risk.planned_loss import planned_stop_loss_usd
from bhiksha.risk.risk_settings import RiskSettings

SHADOW_ENTRY_ORDER_ID = "SHADOW_ENTRY"

TIER1_HALT_REASON = "risk_rail_a_tier1_halt"
TIER2_FLATTEN_REASON = "risk_rail_a_tier2_flatten"
RAIL_B_DEMOTED_REASON = "risk_rail_b_demoted"
CANARY_INHIBITED_REASON = "risk_live_triage_canary_inhibited"
CANARY_INHIBITION_STATE_UNAVAILABLE_REASON = (
    "risk_live_triage_canary_inhibition_state_unavailable"
)
BUDGET_UNAVAILABLE_REASON = "risk_rail_a_budget_unavailable"
OPEN_DRAWDOWN_WARNING_REASON = "risk_open_drawdown_warning"
PROSPECTIVE_LOSS_HEADROOM_REASON = "risk_prospective_loss_headroom_exceeded"
CORRELATION_CLUSTER_CAP_REASON = "risk_correlation_cluster_at_cap"
SIZED_ENTRY_BOOK_UNAVAILABLE_REASON = "risk_sized_entry_book_unavailable"
PROPOSED_STOP_UNAVAILABLE_REASON = "risk_proposed_stop_unavailable"
OPEN_POSITION_RISK_UNAVAILABLE_REASON = "risk_open_position_risk_unavailable"
SIZED_ENTRY_RESERVATION_FAILED_REASON = "risk_sized_entry_reservation_failed"
SIZED_ENTRY_RESERVATION_TTL = timedelta(minutes=30)

# Type of the optional per-position mark-price callback (operator audit P4,
# 2026-07-06 -- see _compute_open_drawdown_status). Keyed by option_symbol,
# returns the current mark (the same "exit reference" premium the native
# exit path already fetches via OrderManager.get_option_quote), or None if a
# quote could not be obtained for that symbol this tick (fail-safe: that
# position is simply excluded from the unrealized sum, never estimated).
MarkPriceProvider = Callable[[str], Awaitable[float | None]]
CanaryZeroFillEvidenceProvider = Callable[[str], Awaitable[bool]]
CanaryProtectionEvidenceProvider = Callable[
    [TradeRecord], Awaitable[bool]
]

# Rail-A-active-but-can't-tell-if-it's-breached reasons (see
# _compute_rail_a_status): only these two mean "the budget/pnl read failed or
# the row does not exist yet". "rail_a_disabled" is a DIFFERENT inactive
# reason (the operator turned the rail off via settings) and must keep
# allowing entries -- see allow_entry's BUDGET_UNAVAILABLE_REASONS check.
BUDGET_UNAVAILABLE_REASONS = frozenset({"no_cash_budget_day", "cash_budget_query_failed"})

# book_actions() is called once per SYMBOL-bar from BhikshaRuntime._handle_bar_event
# (13 symbols x ~1 call/min => ~13x more Rail-A evaluations and event rows than
# needed -- Rail A is a BOOK-level check, not a per-symbol one). Throttle
# constants (2026-07-02 noise-reduction pass, see risk-noise branch):
#   - re-evaluate Rail A at most once per wall-clock minute (matches the
#     1-minute bar cadence -- a new breach is still caught within ~1 tick).
#   - emit an "ok" risk_manager_decision heartbeat at most once per 10
#     minutes OR immediately on any decision/state change; halt/flatten
#     (non-ok) decisions always emit, uncapped.
_EVALUATE_THROTTLE_SECONDS = 60
_OK_HEARTBEAT_SECONDS = 600


def _is_live_trade(trade: TradeRecord) -> bool:
    order_id = trade.entry_order_id or ""
    return order_id != SHADOW_ENTRY_ORDER_ID and not order_id.startswith("DRY_RUN")


def _is_closed_trade(trade: TradeRecord) -> bool:
    return trade.status == "closed"


def _is_open_live_trade(trade: TradeRecord) -> bool:
    """An OPEN live position: a live (non-shadow) trade not yet closed.

    Mirrors ``_is_closed_trade`` (``status == "closed"``) the same way
    ``daily_report._is_open_trade`` does -- "open" is simply "not closed",
    there is no separate open/pending status enum to branch on.
    """
    return _is_live_trade(trade) and not _is_closed_trade(trade)


def _unrealized_pnl_usd(entry_price: float | None, current_mark: float | None, quantity: int | None) -> float | None:
    """Unrealized P&L for one open position, same shape as _realized_pnl_usd.

    This book is always-long-premium (buy calls/puts, quantity is always
    guarded > 0 elsewhere in this codebase -- see TrackedPosition/TradeRecord
    usage in supervisor.py) so there is no separate short-position sign flip:
    the formula is identical to the realized one, entry vs. current instead
    of entry vs. exit.
    """
    if entry_price is None or current_mark is None or not quantity:
        return None
    return round((current_mark - entry_price) * quantity * 100, 2)


def _realized_pnl_usd(trade: TradeRecord) -> float | None:
    if trade.entry_price is None or trade.exit_price is None:
        return None
    quantity = trade.exit_filled_quantity if trade.exit_filled_quantity is not None else trade.quantity
    if not quantity:
        return None
    return round((trade.exit_price - trade.entry_price) * quantity * 100, 2)


def _complete_realized_pnl_usd(
    trade: TradeRecord,
    partials: list[PartialFillRecord],
) -> float | None:
    """Realized P&L across the final residual and confirmed banked legs.

    This intentionally mirrors ``weekly_scorecard._trade_pnl_and_basis``.
    Missing or abandoned partial truth is skipped rather than estimated.
    """
    if trade.entry_price is None:
        return None
    partial_pnl = 0.0
    banked_quantity = 0
    for partial, quantity in _confirmed_partial_legs(trade, partials):
        partial_pnl += (partial.fill_price - trade.entry_price) * quantity * 100
        banked_quantity += quantity

    final_pnl = _realized_pnl_usd(trade)
    if final_pnl is None and banked_quantity == 0:
        return None
    return round((final_pnl or 0.0) + partial_pnl, 2)


def _partial_realized_pnl_usd(
    trade: TradeRecord,
    partial: PartialFillRecord,
    quantity: int,
) -> float | None:
    if trade.entry_price is None or partial.abandoned_reason or partial.fill_price is None:
        return None
    if quantity <= 0:
        return None
    return round((partial.fill_price - trade.entry_price) * quantity * 100, 2)


def _confirmed_partial_legs(
    trade: TradeRecord,
    partials: list[PartialFillRecord],
) -> list[tuple[PartialFillRecord, int]]:
    """Return one bounded, conservative leg per broker order identity."""
    if trade.entry_price is None:
        return []
    selected: dict[tuple[str, object], tuple[PartialFillRecord, int, float]] = {}
    for index, partial in enumerate(partials):
        if partial.abandoned_reason or partial.fill_price is None:
            continue
        closed_quantity = int(partial.closed_quantity or 0)
        reported_quantity = (
            closed_quantity if partial.fill_quantity is None else int(partial.fill_quantity)
        )
        quantity = min(closed_quantity, reported_quantity)
        if quantity <= 0:
            continue
        identity: tuple[str, object]
        if partial.order_id:
            identity = ("order", partial.order_id)
        else:
            identity = ("row", partial.id if partial.id is not None else index)
        pnl = (partial.fill_price - trade.entry_price) * quantity * 100
        current = selected.get(identity)
        # Duplicate broker-order rows are corruption. Count the order once and
        # retain the lower P&L observation so positive duplication cannot hide
        # a halt or demotion.
        if current is None or pnl < current[2]:
            selected[identity] = (partial, quantity, pnl)
    return [(partial, quantity) for partial, quantity, _ in selected.values()]


def _trade_date_et(timestamp: datetime) -> str:
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(et).date().isoformat()


def _as_utc(timestamp: datetime) -> datetime:
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def _trade_is_in_canary_window(
    trade: TradeRecord, policy: dict[str, object]
) -> bool:
    raw_start = str(policy.get("start_at") or "").strip()
    if not raw_start or trade.entry_timestamp is None:
        raise ValueError("canary start_at and trade entry_timestamp are required")
    start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    return _as_utc(trade.entry_timestamp) >= _as_utc(start)


def _canary_policy_time(
    policy: dict[str, object], field_name: str
) -> datetime:
    value = str(policy.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"canary {field_name} is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"canary {field_name} must be timezone-aware")
    return _as_utc(parsed)


def _canary_trade_result(
    trade: TradeRecord,
    partials: list[PartialFillRecord],
    *,
    stop_loss_pct: object,
) -> dict[str, object]:
    """Return complete realized R or an explicit fail-closed evidence state."""

    del stop_loss_pct
    if trade.entry_price is None or trade.entry_price <= 0:
        return {"status": "missing", "reason": "entry_price_missing"}
    frozen_risk = trade.frozen_entry_risk_usd
    if frozen_risk is None or frozen_risk <= 0:
        return {"status": "missing", "reason": "frozen_entry_risk_missing"}
    frozen_cost = trade.frozen_round_trip_cost_usd
    if frozen_cost is None or frozen_cost < 0:
        return {"status": "missing", "reason": "frozen_cost_missing"}
    if trade.exit_order_status and trade.exit_order_status.upper() != "FILLED":
        return {"status": "failed_exit"}

    partial_pnl = 0.0
    banked_quantity = 0
    for partial in partials:
        if partial.abandoned_reason:
            return {"status": "missing", "reason": "partial_fill_abandoned"}
        if not str(partial.exit_rule or "").strip():
            return {"status": "missing", "reason": "partial_exit_attribution_missing"}
        quantity = partial.fill_quantity or partial.closed_quantity
        if partial.fill_price is None or quantity <= 0 or partial.filled_at is None:
            return {"status": "missing", "reason": "partial_fill_truth_missing"}
        partial_pnl += (
            (partial.fill_price - trade.entry_price) * quantity * 100
        )
        banked_quantity += quantity

    final_quantity = (
        trade.exit_filled_quantity
        if trade.exit_filled_quantity is not None
        else trade.quantity
    )
    if final_quantity < 0:
        return {"status": "missing", "reason": "final_quantity_invalid"}
    final_pnl = 0.0
    if final_quantity > 0:
        if (
            trade.exit_order_id is None
            or trade.exit_price is None
            or trade.exit_filled_at is None
            or str(trade.exit_order_status or "").upper() != "FILLED"
        ):
            return {
                "status": "missing",
                "reason": "confirmed_final_exit_truth_missing",
            }
        if not str(trade.exit_rule or "").strip():
            return {"status": "missing", "reason": "final_exit_attribution_missing"}
        final_pnl = (
            (trade.exit_price - trade.entry_price) * final_quantity * 100
        )
    original_quantity = final_quantity + banked_quantity
    if original_quantity <= 0:
        return {"status": "missing", "reason": "original_quantity_missing"}
    after_cost_pnl = final_pnl + partial_pnl - frozen_cost
    return {
        "status": "complete",
        "gross_realized_pnl_usd": round(final_pnl + partial_pnl, 2),
        "frozen_round_trip_cost_usd": round(frozen_cost, 2),
        "realized_pnl_usd": round(after_cost_pnl, 2),
        "frozen_risk_usd": round(frozen_risk, 2),
        "r_multiple": after_cost_pnl / frozen_risk,
    }


@dataclass(slots=True, frozen=True)
class RailAStatus:
    active: bool
    halted: bool
    flatten: bool
    reason: str | None
    realized_live_pnl_usd: float | None = None
    usable_budget: float | None = None
    max_daily_drawdown_pct: float | None = None
    flatten_daily_drawdown_pct: float | None = None
    halt_threshold_usd: float | None = None
    flatten_threshold_usd: float | None = None
    trade_date: str | None = None


@dataclass(slots=True, frozen=True)
class OpenDrawdownStatus:
    """Operator audit P4 (2026-07-06): mark-to-market open-book WARNING.

    WARNING ONLY -- this never halts new entries, never flattens, never
    places/cancels/suppresses an order. It exists because Rail A is
    realized-P&L-only and will not notice an open live position bleeding
    intraday until the loss is realized (native protective stops still
    guard every trade -- this is an awareness gap, not a naked-risk gap).
    """

    active: bool
    breached: bool
    reason: str | None
    realized_usd: float | None = None
    unrealized_open_usd: float | None = None
    day_mtm_usd: float | None = None
    warn_threshold_usd: float | None = None
    warn_pct: float | None = None
    usable_budget: float | None = None
    open_position_count: int | None = None


@dataclass(slots=True, frozen=True)
class RailBStatus:
    demoted: bool
    reason: str | None
    window_n: int = 0
    mean_pnl_usd: float | None = None
    threshold_usd: float | None = None
    newly_demoted: bool = False


@dataclass(slots=True, frozen=True)
class EntryDecision:
    allowed: bool
    reason: str | None = None
    rail: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class BookActionsResult:
    rail_a: RailAStatus
    should_flatten: bool
    flatten_reason: str | None = None
    open_drawdown: OpenDrawdownStatus | None = None


class RiskManager:
    """Pure-ish decision core for Rail A (drawdown halt/flatten) and Rail B (auto-demote).

    Constructor mirrors ``CashGuard``: repositories in, no order-placement
    capability. The caller (``BhikshaRuntime`` / ``ExecutionSupervisor``)
    owns actually flattening positions via the pre-existing
    ``halt_and_flatten_positions`` seam; this class only decides.
    """

    def __init__(
        self,
        *,
        settings: RiskSettings,
        cash_budget_repository: CashBudgetRepository,
        trade_state_repository: TradeStateRepository,
        event_repository: EventRepository,
        demotion_store: DemotionStore | None = None,
        canary_inhibition_store: CanaryInhibitionStore | None = None,
        canary_policies: dict[str, dict[str, object]] | None = None,
        canary_zero_fill_evidence_provider: CanaryZeroFillEvidenceProvider
        | None = None,
        canary_protection_evidence_provider: CanaryProtectionEvidenceProvider
        | None = None,
        alert_mode: AlertMode = "live",
        alert_profile: str | None = None,
        now_fn=None,
        mark_price_provider: MarkPriceProvider | None = None,
    ) -> None:
        self.settings = settings
        self.cash_budget_repository = cash_budget_repository
        self.trade_state_repository = trade_state_repository
        self.event_repository = event_repository
        self.demotion_store = demotion_store or DemotionStore()
        self.canary_inhibition_store = (
            canary_inhibition_store or CanaryInhibitionStore()
        )
        self.canary_policies = {
            str(deployment_id): dict(policy)
            for deployment_id, policy in (canary_policies or {}).items()
        }
        self._canary_zero_fill_evidence_provider = (
            canary_zero_fill_evidence_provider
        )
        self._canary_protection_evidence_provider = (
            canary_protection_evidence_provider
        )
        self.alert_mode = alert_mode
        self.alert_profile = alert_profile
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        # Operator audit P4 (2026-07-06): optional per-option-symbol mark
        # fetch, reusing the SAME broker quote flow the native exit path
        # already uses (OrderManager.get_option_quote) -- there is no
        # existing per-tick cache of live marks reachable from book_actions()
        # without this seam (see risk-a5-mtm-warning branch notes / PR
        # description for the recon). None (the default) means the feature
        # is INACTIVE: _compute_open_drawdown_status always returns
        # active=False, never a spurious warning -- fail-safe, matching Rail
        # A's own missing-data posture.
        self._mark_price_provider = mark_price_provider
        # Session-scoped: once Rail A halts, stay halted for the rest of the
        # session even if a subsequent P&L read is momentarily better (no
        # flip-flopping new entries back on intraday). Tier 2 flatten is
        # similarly session-latched to avoid repeated flatten submissions.
        self._session_halted = False
        self._session_flattened = False
        # Newly-demoted-this-session ids, so allow_entry can block them
        # immediately (consult-point decision) even before the compiler
        # reruns on the override file at next compile.
        self._session_demoted_ids: set[str] = set()
        self._notified_tier1 = False
        self._notified_tier2 = False
        # Operator audit P4: same "once per session" latch as tier1/tier2 --
        # never clears once fired, even if a later mark makes the book look
        # healthy again intraday (matches the existing rail posture: act
        # once, stay acted/notified).
        self._notified_open_drawdown_warning = False
        # Rail-A evaluate-throttle cache (see _EVALUATE_THROTTLE_SECONDS):
        # book_actions() is called once per symbol-bar (13x/minute) but Rail A
        # is a book-level check -- cache the result for the rest of the
        # wall-clock minute instead of recomputing/re-notifying per symbol.
        self._book_actions_cache_minute: int | None = None
        self._book_actions_cache_result: BookActionsResult | None = None
        # "ok" event heartbeat throttle (see _OK_HEARTBEAT_SECONDS): always
        # emit on a decision/state change or a non-ok decision; otherwise cap
        # "ok" rows to one per heartbeat window so the event log stays
        # readable without losing the halt/flatten/change signal.
        self._last_emitted_decision: str | None = None
        self._last_ok_heartbeat_at: datetime | None = None
        # A broker submission happens after the final quote/preflight. Keep
        # the risk check and reservation atomic so concurrent signal runners
        # cannot both observe the same headroom. The reservation remains until
        # the supervisor has persisted the filled/open trade, or releases it
        # after a confirmed no-fill path.
        self._sized_entry_lock = asyncio.Lock()

    async def startup_log(self) -> None:
        """Emit the one startup event proving resolved knobs (env > sheet > default)."""
        await self.event_repository.append("risk_manager_startup", self.settings.to_dict())

    # ------------------------------------------------------------------ #
    # Rail A: portfolio daily-drawdown cap
    # ------------------------------------------------------------------ #

    async def _compute_rail_a_status(self) -> RailAStatus:
        if not self.settings.rail_a_enabled:
            return RailAStatus(active=False, halted=False, flatten=False, reason="rail_a_disabled")

        now = self._now_fn()
        trade_date = _trade_date_et(now)

        try:
            budget_day = await self.cash_budget_repository.get_day(trade_date)
        except Exception as exc:  # fail-safe: missing data never spuriously halts
            await self.event_repository.append(
                "risk_manager_decision",
                {
                    "rail": "A",
                    "decision": "inactive",
                    "severity": "warning",
                    "reason": "cash_budget_query_failed",
                    "error": str(exc),
                    "trade_date": trade_date,
                },
            )
            return RailAStatus(active=False, halted=False, flatten=False, reason="cash_budget_query_failed", trade_date=trade_date)

        if budget_day is None:
            await self.event_repository.append(
                "risk_manager_decision",
                {
                    "rail": "A",
                    "decision": "inactive",
                    "severity": "warning",
                    "reason": "no_cash_budget_day",
                    "trade_date": trade_date,
                },
            )
            return RailAStatus(active=False, halted=False, flatten=False, reason="no_cash_budget_day", trade_date=trade_date)

        try:
            realized_pnl = await self._realized_live_pnl_today(trade_date)
        except Exception as exc:  # fail-safe: a P&L query failure is INACTIVE, not a halt
            await self.event_repository.append(
                "risk_manager_decision",
                {
                    "rail": "A",
                    "decision": "inactive",
                    "severity": "warning",
                    "reason": "pnl_query_failed",
                    "error": str(exc),
                    "trade_date": trade_date,
                },
            )
            return RailAStatus(active=False, halted=False, flatten=False, reason="pnl_query_failed", trade_date=trade_date)

        usable_budget = budget_day.usable_budget
        halt_threshold = -(self.settings.max_daily_drawdown_pct / 100.0) * usable_budget
        flatten_threshold = -(self.settings.flatten_daily_drawdown_pct / 100.0) * usable_budget

        halted = realized_pnl <= halt_threshold
        flatten = realized_pnl <= flatten_threshold

        return RailAStatus(
            active=True,
            halted=halted,
            flatten=flatten,
            reason=(TIER2_FLATTEN_REASON if flatten else (TIER1_HALT_REASON if halted else None)),
            realized_live_pnl_usd=realized_pnl,
            usable_budget=usable_budget,
            max_daily_drawdown_pct=self.settings.max_daily_drawdown_pct,
            flatten_daily_drawdown_pct=self.settings.flatten_daily_drawdown_pct,
            halt_threshold_usd=round(halt_threshold, 2),
            flatten_threshold_usd=round(flatten_threshold, 2),
            trade_date=trade_date,
        )

    async def _realized_live_pnl_today(self, trade_date: str) -> float:
        trades = await self.trade_state_repository.get_recent_trades(limit=1000)
        relevant_trades = [
            trade
            for trade in trades
            if _is_live_trade(trade)
            and not (
                _is_closed_trade(trade)
                and (
                    trade.exit_filled_at is None
                    or _trade_date_et(trade.exit_filled_at) != trade_date
                )
            )
        ]
        partials_by_trade = await self.trade_state_repository.get_partial_fills_for_trades(
            [trade.trade_id for trade in relevant_trades]
        )
        total = 0.0
        for trade in relevant_trades:
            final_filled_today = (
                _is_closed_trade(trade)
                and trade.exit_filled_at is not None
                and _trade_date_et(trade.exit_filled_at) == trade_date
            )
            if final_filled_today:
                final_pnl = _realized_pnl_usd(trade)
                if final_pnl is not None:
                    total += final_pnl
            for partial, quantity in _confirmed_partial_legs(
                trade, partials_by_trade.get(trade.trade_id, [])
            ):
                if partial.filled_at is None or _trade_date_et(partial.filled_at) != trade_date:
                    continue
                partial_pnl = _partial_realized_pnl_usd(trade, partial, quantity)
                if partial_pnl is not None:
                    total += partial_pnl
        return round(total, 2)

    @property
    def effective_open_drawdown_warn_pct(self) -> float:
        """The resolved warn pct: an explicit setting, else tier-1's pct.

        Applied at point-of-use (not baked into ``resolve_risk_settings``) so
        a directly-constructed ``RiskSettings`` -- e.g. in tests, without this
        field -- gets the identical "unset -> tier-1" fallback as the env/
        sheet-resolved path.
        """
        if self.settings.open_drawdown_warn_pct is not None:
            return self.settings.open_drawdown_warn_pct
        return self.settings.max_daily_drawdown_pct

    # ------------------------------------------------------------------ #
    # Operator audit P4 (2026-07-06): mark-to-market open-book WARNING.
    # WARNING ONLY -- see OpenDrawdownStatus docstring. Combines TODAY's
    # realized live P&L (the exact same _realized_live_pnl_today Rail A
    # uses) with the CURRENT unrealized P&L of every OPEN live position
    # (mark - entry_price) * quantity * 100, same formula as
    # _realized_pnl_usd/_unrealized_pnl_usd, just entry-vs-current instead of
    # entry-vs-exit. Fail-safe: any missing mark, missing budget, or a rail
    # already realized-halted skips silently -- never a spurious warning.
    # ------------------------------------------------------------------ #

    async def _compute_open_drawdown_status(
        self, *, realized_pnl: float, usable_budget: float, trade_date: str, rail_a_already_breached: bool
    ) -> OpenDrawdownStatus:
        if not self.settings.rail_a_enabled:
            return OpenDrawdownStatus(active=False, breached=False, reason="rail_a_disabled")
        if self._mark_price_provider is None:
            # No quote seam wired up (e.g. a test/tooling context, or the
            # runtime not yet threading OrderManager through) -- INACTIVE,
            # never a spurious warning. This mirrors Rail A's own fail-safe
            # "missing data -> inactive" posture, not fail-open.
            return OpenDrawdownStatus(active=False, breached=False, reason="mark_price_provider_unavailable")

        try:
            trades = await self.trade_state_repository.get_recent_trades(limit=1000)
        except Exception:
            return OpenDrawdownStatus(active=False, breached=False, reason="pnl_query_failed")

        open_live_trades = [trade for trade in trades if _is_open_live_trade(trade)]
        if not open_live_trades:
            # No open live positions: unrealized is unambiguously zero, no
            # marks to fetch. Still evaluate against realized-only so a
            # WARNING can never be silently skipped just because the book
            # happens to be flat right now (the day MTM equals realized).
            day_mtm = realized_pnl
            warn_pct = self.effective_open_drawdown_warn_pct
            warn_threshold = -(warn_pct / 100.0) * usable_budget
            breached = day_mtm <= warn_threshold and not rail_a_already_breached
            return OpenDrawdownStatus(
                active=True,
                breached=breached,
                reason=OPEN_DRAWDOWN_WARNING_REASON if breached else None,
                realized_usd=realized_pnl,
                unrealized_open_usd=0.0,
                day_mtm_usd=day_mtm,
                warn_threshold_usd=round(warn_threshold, 2),
                warn_pct=warn_pct,
                usable_budget=usable_budget,
                open_position_count=0,
            )

        unrealized_total = 0.0
        priced_count = 0
        for trade in open_live_trades:
            if trade.option_symbol is None:
                continue
            try:
                mark = await self._mark_price_provider(trade.option_symbol)
            except Exception:
                mark = None
            pnl = _unrealized_pnl_usd(trade.entry_price, mark, trade.quantity)
            if pnl is None:
                # Missing mark for this one position -- fail-safe: exclude it
                # from the sum rather than guessing/estimating. If EVERY open
                # position is missing a mark, priced_count stays 0 and the
                # whole check goes inactive below (never a spurious warning
                # built from zero real data).
                continue
            unrealized_total += pnl
            priced_count += 1

        if priced_count == 0:
            return OpenDrawdownStatus(active=False, breached=False, reason="no_priced_open_positions")

        unrealized_total = round(unrealized_total, 2)
        day_mtm = round(realized_pnl + unrealized_total, 2)
        warn_pct = self.effective_open_drawdown_warn_pct
        warn_threshold = -(warn_pct / 100.0) * usable_budget
        breached = day_mtm <= warn_threshold and not rail_a_already_breached

        return OpenDrawdownStatus(
            active=True,
            breached=breached,
            reason=OPEN_DRAWDOWN_WARNING_REASON if breached else None,
            realized_usd=realized_pnl,
            unrealized_open_usd=unrealized_total,
            day_mtm_usd=day_mtm,
            warn_threshold_usd=round(warn_threshold, 2),
            warn_pct=warn_pct,
            usable_budget=usable_budget,
            open_position_count=priced_count,
        )

    async def book_actions(self) -> BookActionsResult:
        """Periodic/tick consult point: recompute Rail A and report a flatten decision.

        Does NOT flatten anything itself -- returns ``should_flatten`` so the
        caller invokes the existing ``halt_and_flatten_positions`` seam. Rate
        limited to one notification per tier per session (see
        ``_notified_tier1``/``_notified_tier2``) so this is safe to call once
        per tick-batch without spamming Telegram.

        EVALUATE THROTTLE: this is called once per SYMBOL-bar by
        ``BhikshaRuntime._handle_bar_event`` (13 symbols/minute in
        production), but Rail A is a BOOK-level check -- recomputing it 13x
        a minute is redundant. Cache the result per wall-clock minute (from
        ``self._now_fn()``, the same clock ``_compute_rail_a_status`` already
        uses -- not bar-timestamp parsing) and return the cached
        ``BookActionsResult`` for any call within the same minute. A new
        breach is still caught within ~1 minute worst case, matching the
        1-minute bar cadence this already ran at. The flatten latch itself
        (``_session_flattened``) is untouched by the cache: once set it
        never clears, cached or not.
        """
        minute_key = int(self._now_fn().timestamp() // _EVALUATE_THROTTLE_SECONDS)
        if self._book_actions_cache_minute == minute_key and self._book_actions_cache_result is not None:
            return self._book_actions_cache_result

        result = await self._book_actions_uncached()
        self._book_actions_cache_minute = minute_key
        self._book_actions_cache_result = result
        return result

    async def _book_actions_uncached(self) -> BookActionsResult:
        status = await self._compute_rail_a_status()
        if status.active and status.halted:
            self._session_halted = True
        if status.active and status.flatten:
            self._session_flattened = True

        # _compute_rail_a_status already emitted the one warning event when
        # Rail A is INACTIVE (missing budget day / query failure); do not
        # double-log here, just report the tick's ok/halt/flatten decision
        # while Rail A is actually active and evaluated.
        #
        # EVENT-ROW POLICY (2026-07-02 noise-reduction pass): non-ok
        # decisions (halt/flatten) and any decision/state CHANGE always
        # emit, uncapped -- that is the actionable signal. A repeated "ok"
        # decision is a heartbeat/proof-of-life row, capped to once per
        # _OK_HEARTBEAT_SECONDS so the event log stays readable.
        if status.active:
            decision = "flatten" if status.flatten else ("halt" if status.halted else "ok")
            changed = decision != self._last_emitted_decision
            now = self._now_fn()
            heartbeat_due = (
                self._last_ok_heartbeat_at is None
                or (now - self._last_ok_heartbeat_at).total_seconds() >= _OK_HEARTBEAT_SECONDS
            )
            should_emit = decision != "ok" or changed or heartbeat_due
            if should_emit:
                await self.event_repository.append(
                    "risk_manager_decision",
                    {
                        "rail": "A",
                        "decision": decision,
                        "active": status.active,
                        "realized_live_pnl_usd": status.realized_live_pnl_usd,
                        "usable_budget": status.usable_budget,
                        "halt_threshold_usd": status.halt_threshold_usd,
                        "flatten_threshold_usd": status.flatten_threshold_usd,
                        "trade_date": status.trade_date,
                    },
                )
                self._last_emitted_decision = decision
                if decision == "ok":
                    self._last_ok_heartbeat_at = now

        if status.active and status.halted and not self._notified_tier1:
            self._notified_tier1 = True
            await self._notify(
                title="Rail A tier-1 daily drawdown halt",
                body=(
                    f"Realized live P&L today: ${status.realized_live_pnl_usd:.2f} "
                    f"<= halt threshold ${status.halt_threshold_usd:.2f} "
                    f"(usable budget ${status.usable_budget:.2f}, "
                    f"max_daily_drawdown_pct={status.max_daily_drawdown_pct}). "
                    "New live entries are HALTED for the rest of the session."
                ),
                level="error",
            )
        if status.active and status.flatten and not self._notified_tier2:
            self._notified_tier2 = True
            await self._notify(
                title="Rail A tier-2 daily drawdown FLATTEN",
                body=(
                    f"Realized live P&L today: ${status.realized_live_pnl_usd:.2f} "
                    f"<= flatten threshold ${status.flatten_threshold_usd:.2f} "
                    f"(usable budget ${status.usable_budget:.2f}, "
                    f"flatten_daily_drawdown_pct={status.flatten_daily_drawdown_pct}). "
                    "Open live positions are being flattened."
                ),
                level="error",
            )

        # Operator audit P4 (2026-07-06): mark-to-market open-book WARNING.
        # Only evaluated when Rail A itself is ACTIVE (a real usable_budget
        # and realized P&L are already in hand from _compute_rail_a_status
        # above) -- when Rail A is inactive (missing budget/pnl query
        # failure/disabled), _compute_rail_a_status already emitted its own
        # fail-safe warning and there is nothing safe to combine, so this
        # stays untouched (open_drawdown=None on the result).
        open_drawdown: OpenDrawdownStatus | None = None
        if status.active:
            open_drawdown = await self._compute_open_drawdown_status(
                realized_pnl=status.realized_live_pnl_usd or 0.0,
                usable_budget=status.usable_budget,
                trade_date=status.trade_date,
                rail_a_already_breached=status.halted,
            )
            if open_drawdown.active and open_drawdown.breached and not self._notified_open_drawdown_warning:
                self._notified_open_drawdown_warning = True
                await self.event_repository.append(
                    OPEN_DRAWDOWN_WARNING_REASON,
                    {
                        "realized_usd": open_drawdown.realized_usd,
                        "unrealized_open_usd": open_drawdown.unrealized_open_usd,
                        "day_mtm_usd": open_drawdown.day_mtm_usd,
                        "warn_threshold_usd": open_drawdown.warn_threshold_usd,
                        "warn_pct": open_drawdown.warn_pct,
                        "usable_budget": open_drawdown.usable_budget,
                        "open_position_count": open_drawdown.open_position_count,
                        "trade_date": status.trade_date,
                    },
                )
                await self._notify(
                    title="Rail A open-book mark-to-market WARNING",
                    body=(
                        f"Day mark-to-market P&L: ${open_drawdown.day_mtm_usd:.2f} "
                        f"(realized ${open_drawdown.realized_usd:.2f} + unrealized open "
                        f"${open_drawdown.unrealized_open_usd:.2f} across "
                        f"{open_drawdown.open_position_count} open live position(s)) "
                        f"<= warn threshold ${open_drawdown.warn_threshold_usd:.2f} "
                        f"(usable budget ${open_drawdown.usable_budget:.2f}, "
                        f"open_drawdown_warn_pct={open_drawdown.warn_pct}). "
                        "WARNING ONLY -- no order has been placed, canceled, or "
                        "suppressed. Realized-only Rail A has not itself halted; an "
                        "open position is bleeding intraday and native protective "
                        "stops remain the sole automatic guard on it."
                    ),
                    level="warning",
                )

        return BookActionsResult(
            rail_a=status,
            should_flatten=self._session_flattened,
            flatten_reason=TIER2_FLATTEN_REASON if self._session_flattened else None,
            open_drawdown=open_drawdown,
        )

    # ------------------------------------------------------------------ #
    # Rail B: per-deployment auto-demote
    # ------------------------------------------------------------------ #

    async def _evaluate_rail_b(self, deployment_id: str) -> RailBStatus:
        if not self.settings.rail_b_enabled:
            return RailBStatus(demoted=False, reason="rail_b_disabled")

        if deployment_id in self._session_demoted_ids or self.demotion_store.is_demoted(deployment_id):
            self._session_demoted_ids.add(deployment_id)
            return RailBStatus(demoted=True, reason=RAIL_B_DEMOTED_REASON)

        trades = await self.trade_state_repository.get_recent_trades(limit=1000)
        deployment_live_closed = [
            trade
            for trade in trades
            if trade.deployment_id == deployment_id and _is_live_trade(trade) and _is_closed_trade(trade)
        ]
        cutoff = self.demotion_store.repromotion_cutoff(deployment_id)
        if cutoff is not None:
            deployment_live_closed = [
                trade
                for trade in deployment_live_closed
                if trade.exit_filled_at is not None
                and _as_utc(trade.exit_filled_at) > cutoff
            ]
        # get_recent_trades is ordered by updated_at DESC. Build the rolling
        # window from the latest trades with complete realized economics,
        # rather than slicing raw ``status='closed'`` rows first. A legacy or
        # corrupt closed row with no exit truth must not permanently consume
        # an evidence slot and prevent Rail B from seeing older priced trades.
        if len(deployment_live_closed) < self.settings.demote_min_n:
            return RailBStatus(
                demoted=False,
                reason="insufficient_trade_count",
                window_n=len(deployment_live_closed),
            )
        partials_by_trade = await self.trade_state_repository.get_partial_fills_for_trades(
            [trade.trade_id for trade in deployment_live_closed]
        )
        pnls = []
        priced_trade_ids = []
        for trade in deployment_live_closed:
            partials = partials_by_trade.get(trade.trade_id, [])
            pnl = _complete_realized_pnl_usd(trade, partials)
            if pnl is not None:
                pnls.append(pnl)
                priced_trade_ids.append(trade.trade_id)
                if len(pnls) == self.settings.demote_window:
                    break
        if len(pnls) < self.settings.demote_min_n:
            return RailBStatus(demoted=False, reason="insufficient_priced_trade_count", window_n=len(pnls))

        mean_pnl = round(sum(pnls) / len(pnls), 2)
        if mean_pnl >= self.settings.demote_threshold_usd:
            return RailBStatus(demoted=False, reason=None, window_n=len(pnls), mean_pnl_usd=mean_pnl, threshold_usd=self.settings.demote_threshold_usd)

        record = self.demotion_store.record_demotion(
            deployment_id=deployment_id,
            reason="rolling_window_negative_expectancy",
            window_n=len(pnls),
            mean_pnl_usd=mean_pnl,
            threshold_usd=self.settings.demote_threshold_usd,
            trade_ids=priced_trade_ids,
            now=self._now_fn(),
        )
        self._session_demoted_ids.add(deployment_id)

        await self.event_repository.append(
            "risk_manager_demotion",
            {
                "deployment_id": deployment_id,
                "window_n": record.window_n,
                "mean_pnl_usd": record.mean_pnl_usd,
                "threshold_usd": record.threshold_usd,
                "demoted_at": record.demoted_at,
            },
        )
        await self._notify(
            title=f"Rail B auto-demote: {deployment_id}",
            body=(
                f"Deployment {deployment_id} demoted live->shadow. "
                f"Last {record.window_n} closed live trades averaged "
                f"${record.mean_pnl_usd:.2f}/trade (threshold ${record.threshold_usd:.2f}). "
                "Live entries for this deployment are blocked for the rest of this session "
                "and it will compile shadow-only starting next active-plan compile. "
                "There is no automatic re-promotion. A protected operator reset "
                "is required and starts a fresh Rail B evidence window."
            ),
            level="warning",
        )
        return RailBStatus(
            demoted=True,
            reason=RAIL_B_DEMOTED_REASON,
            window_n=record.window_n,
            mean_pnl_usd=record.mean_pnl_usd,
            threshold_usd=record.threshold_usd,
            newly_demoted=True,
        )

    # ------------------------------------------------------------------ #
    # Consult point: entry-planning seam
    # ------------------------------------------------------------------ #

    async def allow_entry(self, deployment_id: str) -> EntryDecision:
        """Entry-planning consult point.

        Returns ``EntryDecision(allowed=False, reason=...)`` the same shape
        the runtime already threads through
        ``ExecutionSupervisor.handle_signal(live_entry_block_reason=...)``:
        callers can do ``live_entry_block_reason=decision.reason`` directly.
        Every call emits exactly one ``risk_manager_decision`` event (the
        production proof surface), even when the entry is allowed.

        Tier-1 halt is session-latched via ``_session_halted``: once a
        computed breach has fired once this session, entries stay blocked
        for the rest of the session even if a later P&L recompute is
        momentarily back above threshold (e.g. a late fill correction) --
        the same "act once, stay acted" posture as tier-2 flatten and Rail
        B's demotion. A HALT should never silently flip back to OK mid-day.

        UNKNOWN BUDGET BLOCKS NEW ENTRIES (operator audit P2, 2026-07-03
        finding): when Rail A is ENABLED but ``_compute_rail_a_status``
        cannot tell whether it's breached -- no ``cash_budget_days`` row for
        today yet (``no_cash_budget_day``), or the budget/pnl read itself
        failed (``cash_budget_query_failed``) -- this returns
        ``allowed=False, reason=BUDGET_UNAVAILABLE_REASON`` instead of
        falling through to allowed. This DELIBERATELY flips the prior
        fail-safe posture for NEW entries only: unknown budget must not
        flatten anything (``book_actions`` / ``_compute_rail_a_status``
        stay ``active=False``, no computed breach, no flatten -- unchanged),
        but it must not let a live entry through blind either -- on
        2026-07-03 a live SMH entry was allowed during a 6-minute window
        where the budget row simply hadn't been created yet. This check is
        NOT latched (unlike tier-1 halt): as soon as the row exists again
        (e.g. the startup prefetch or the next lazy-create succeeds),
        entries resume being evaluated normally.

        When Rail A is explicitly DISABLED via settings
        (``rail_a_enabled=False`` -> reason ``"rail_a_disabled"``), that is a
        distinct inactive reason and entries continue to be ALLOWED -- the
        operator turned the rail off on purpose, unlike an unknown budget.
        """
        rail_a = await self._compute_rail_a_status()
        # Audit fix (2026-07-02): tier-2 flatten IMPLIES tier-1's entry block.
        # Settings validation now clamps flatten >= halt, but this is the
        # independent backstop: a flattening book must never accept new
        # entries regardless of how the thresholds were configured.
        if rail_a.active and (rail_a.halted or rail_a.flatten):
            self._session_halted = True
        if self._session_halted:
            decision = EntryDecision(
                allowed=False,
                reason=TIER1_HALT_REASON,
                rail="A",
                details={
                    "realized_live_pnl_usd": rail_a.realized_live_pnl_usd,
                    "halt_threshold_usd": rail_a.halt_threshold_usd,
                },
            )
            await self._emit_entry_decision(deployment_id, decision)
            return decision

        if self.settings.rail_a_enabled and not rail_a.active and rail_a.reason in BUDGET_UNAVAILABLE_REASONS:
            decision = EntryDecision(
                allowed=False,
                reason=BUDGET_UNAVAILABLE_REASON,
                rail="A",
                details={"rail_a_inactive_reason": rail_a.reason, "trade_date": rail_a.trade_date},
            )
            await self._emit_entry_decision(deployment_id, decision)
            return decision

        decision = await self._canary_entry_decision(deployment_id)
        if decision is not None:
            await self._emit_entry_decision(deployment_id, decision)
            return decision

        rail_b = await self._evaluate_rail_b(deployment_id)
        if rail_b.demoted:
            decision = EntryDecision(
                allowed=False,
                reason=RAIL_B_DEMOTED_REASON,
                rail="B",
                details={
                    "window_n": rail_b.window_n,
                    "mean_pnl_usd": rail_b.mean_pnl_usd,
                    "threshold_usd": rail_b.threshold_usd,
                },
            )
            await self._emit_entry_decision(deployment_id, decision)
            return decision

        decision = EntryDecision(allowed=True, reason="approved")
        await self._emit_entry_decision(deployment_id, decision)
        return decision

    async def _canary_entry_decision(
        self, deployment_id: str
    ) -> EntryDecision | None:
        automatic_canary_block = await self._evaluate_live_triage_canary(
            deployment_id
        )
        if automatic_canary_block is not None:
            return automatic_canary_block
        try:
            canary_inhibitions = self.canary_inhibition_store.matching(
                deployment_id
            )
        except CanaryInhibitionStoreError as exc:
            return EntryDecision(
                allowed=False,
                reason=CANARY_INHIBITION_STATE_UNAVAILABLE_REASON,
                rail="CANARY",
                details={"error": str(exc)},
            )
        if not canary_inhibitions:
            return None
        return EntryDecision(
            allowed=False,
            reason=CANARY_INHIBITED_REASON,
            rail="CANARY",
            details={
                "canary_ids": [record.canary_id for record in canary_inhibitions],
                "latched_at": [record.latched_at for record in canary_inhibitions],
                "inhibition_reasons": [
                    record.reason for record in canary_inhibitions
                ],
            },
        )

    async def _evaluate_live_triage_canary(
        self, deployment_id: str
    ) -> EntryDecision | None:
        """Create durable stop latches before another canary entry is allowed."""

        policy = self.canary_policies.get(deployment_id)
        if policy is None:
            return None
        canary_id = str(policy.get("canary_id") or "").strip()
        if not canary_id:
            return EntryDecision(
                allowed=False,
                reason=CANARY_INHIBITION_STATE_UNAVAILABLE_REASON,
                rail="CANARY",
                details={"error": "canary_id_missing"},
            )
        try:
            start = _canary_policy_time(policy, "start_at")
            expires = _canary_policy_time(policy, "expires_at")
            now = _as_utc(self._now_fn())
            if now < start or now > expires:
                return self._latch_canary_stop(
                    deployment_id=deployment_id,
                    canary_id=canary_id,
                    reason="authorization_window_inactive",
                    evidence={
                        "evaluated_at": now.isoformat(),
                        "start_at": start.isoformat(),
                        "expires_at": expires.isoformat(),
                    },
                )
            open_trades = await self.trade_state_repository.get_open_trades()
            for trade in open_trades:
                if (
                    trade.deployment_id == deployment_id
                    and _is_live_trade(trade)
                    and trade.status not in {"pending_entry", "entry_reconciliation_hold"}
                    and (trade.entry_price or 0) > 0
                    and trade.quantity > 0
                ):
                    protection_proved = False
                    if self._canary_protection_evidence_provider is not None:
                        protection_proved = bool(
                            await self._canary_protection_evidence_provider(trade)
                        )
                    if not protection_proved:
                        return self._latch_canary_stop(
                            deployment_id=deployment_id,
                            canary_id=canary_id,
                            reason="unprotected_position",
                            evidence={
                                "trade_id": trade.trade_id,
                                "status": trade.status,
                                "stop_order_id": trade.stop_order_id,
                                "protection_proof": "broker_working_status_missing",
                            },
                        )

            recent = await self.trade_state_repository.get_recent_trades(
                limit=1000
            )
            closed = [
                trade
                for trade in recent
                if trade.deployment_id == deployment_id
                and _is_live_trade(trade)
                and _is_closed_trade(trade)
                and _trade_is_in_canary_window(trade, policy)
            ]
            partials_by_trade = (
                await self.trade_state_repository.get_partial_fills_for_trades(
                    [trade.trade_id for trade in closed]
                )
                if closed
                else {}
            )
            cumulative_r = 0.0
            contributing_trade_ids: list[str] = []
            for trade in reversed(closed):
                if await self._is_confirmed_zero_fill(trade.trade_id):
                    continue
                result = _canary_trade_result(
                    trade,
                    partials_by_trade.get(trade.trade_id, []),
                    stop_loss_pct=policy.get("stop_loss_pct"),
                )
                if result["status"] == "missing":
                    return self._latch_canary_stop(
                        deployment_id=deployment_id,
                        canary_id=canary_id,
                        reason="missing_trade_or_exit_attribution",
                        evidence={
                            "trade_id": trade.trade_id,
                            "missing_reason": result.get("reason"),
                        },
                    )
                if result["status"] == "failed_exit":
                    return self._latch_canary_stop(
                        deployment_id=deployment_id,
                        canary_id=canary_id,
                        reason="failed_exit_receipt",
                        evidence={
                            "trade_id": trade.trade_id,
                            "exit_order_status": trade.exit_order_status,
                        },
                    )
                cumulative_r += float(result["r_multiple"])
                contributing_trade_ids.append(trade.trade_id)

            loss_floor = float(policy.get("max_cumulative_loss_r", -2.0))
            if cumulative_r <= loss_floor:
                return self._latch_canary_stop(
                    deployment_id=deployment_id,
                    canary_id=canary_id,
                    reason="cumulative_loss_r",
                    evidence={
                        "cumulative_r": round(cumulative_r, 6),
                        "loss_floor_r": loss_floor,
                        "trade_ids": contributing_trade_ids,
                        "r_definition": (
                            "sum(realized_total_pnl_usd / "
                            "frozen_entry_premium_stop_risk_usd)"
                        ),
                    },
                )
        except CanaryInhibitionStoreError as exc:
            return EntryDecision(
                allowed=False,
                reason=CANARY_INHIBITION_STATE_UNAVAILABLE_REASON,
                rail="CANARY",
                details={"error": str(exc)},
            )
        except Exception as exc:
            return EntryDecision(
                allowed=False,
                reason=CANARY_INHIBITION_STATE_UNAVAILABLE_REASON,
                rail="CANARY",
                details={"error": f"canary_evidence_evaluation_failed:{exc}"},
            )
        return None

    async def _is_confirmed_zero_fill(self, trade_id: str) -> bool:
        if self._canary_zero_fill_evidence_provider is None:
            return False
        return bool(await self._canary_zero_fill_evidence_provider(trade_id))

    def _latch_canary_stop(
        self,
        *,
        deployment_id: str,
        canary_id: str,
        reason: str,
        evidence: dict[str, object],
    ) -> EntryDecision:
        record = self.canary_inhibition_store.record_inhibition(
            deployment_id=deployment_id,
            canary_id=canary_id,
            reason=reason,
            evidence=evidence,
            now=self._now_fn(),
        )
        return EntryDecision(
            allowed=False,
            reason=CANARY_INHIBITED_REASON,
            rail="CANARY",
            details={
                "canary_ids": [record.canary_id],
                "latched_at": [record.latched_at],
                "inhibition_reasons": [record.reason],
                "evidence": record.evidence,
            },
        )

    async def reserve_sized_entry(
        self,
        *,
        trade_id: str,
        deployment_id: str,
        symbol: str,
        entry_price: float,
        quantity: int,
        stop_loss_pct: float | None,
    ) -> EntryDecision:
        """Atomically approve and reserve final, priced live-entry risk.

        Rail A's realized-only halt remains the backstop. This consult prevents
        the book from accepting open planned-stop risk which, together with
        losses already realized today, would exceed that same halt budget.
        """
        async with self._sized_entry_lock:
            # This is the final pre-submission lock used by the planner.  The
            # canary window, evidence-based stop conditions, and durable latch
            # must be re-read here—not only in the earlier planning consult—so
            # expiry or a concurrent stop cannot race an order submission.
            canary_decision = await self._canary_entry_decision(deployment_id)
            if canary_decision is not None:
                await self._emit_sized_entry_decision(
                    deployment_id, trade_id, canary_decision
                )
                return canary_decision
            proposed_loss = planned_stop_loss_usd(
                entry_price=entry_price,
                quantity=quantity,
                stop_loss_pct=stop_loss_pct,
            )
            if proposed_loss is None and self.settings.prospective_loss_enabled:
                decision = EntryDecision(
                    allowed=False,
                    reason=PROPOSED_STOP_UNAVAILABLE_REASON,
                    rail="A-prospective",
                    details={
                        "entry_price": entry_price,
                        "quantity": quantity,
                        "stop_loss_pct": stop_loss_pct,
                    },
                )
                await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                return decision
            proposed_loss = proposed_loss or 0.0

            cluster = correlation_cluster(symbol)
            try:
                open_trades = await self.trade_state_repository.get_open_trades()
                active_reservations = (
                    await self.trade_state_repository.get_active_entry_risk_reservations()
                )
            except Exception as exc:
                decision = EntryDecision(
                    allowed=False,
                    reason=SIZED_ENTRY_BOOK_UNAVAILABLE_REASON,
                    rail="entry-risk",
                    details={"error": str(exc)},
                )
                await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                return decision

            now = self._now_fn()
            other_reservations: dict[str, EntryRiskReservation] = {}
            for reservation in active_reservations:
                if reservation.expires_at <= now:
                    await self.trade_state_repository.mark_entry_risk_reservation_status(
                        reservation.trade_id,
                        "expired",
                    )
                    continue
                if reservation.trade_id != trade_id:
                    other_reservations[reservation.trade_id] = reservation
            reserved_ids = set(other_reservations)
            live_open_trades = [
                trade
                for trade in open_trades
                if _is_open_live_trade(trade)
                and trade.trade_id != trade_id
                and trade.trade_id not in reserved_ids
            ]

            cluster_count = sum(
                1
                for trade in live_open_trades
                if correlation_cluster(trade.symbol) == cluster
            ) + sum(
                1
                for reservation in other_reservations.values()
                if reservation.cluster == cluster
            )
            cluster_limit = self.settings.max_open_positions_per_cluster
            cluster_details: dict[str, object] = {
                "correlation_cluster": cluster,
                "cluster_open_or_reserved_count": cluster_count,
                "max_open_positions_per_cluster": cluster_limit,
                "cluster_mapped": cluster is not None,
            }
            if cluster is not None and cluster_limit > 0 and cluster_count >= cluster_limit:
                decision = EntryDecision(
                    allowed=False,
                    reason=CORRELATION_CLUSTER_CAP_REASON,
                    rail="correlation-cluster",
                    details=cluster_details,
                )
                await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                return decision

            loss_details: dict[str, object] = {
                "prospective_loss_enabled": self.settings.prospective_loss_enabled,
                "proposed_planned_stop_loss_usd": proposed_loss,
                **cluster_details,
            }
            if self.settings.prospective_loss_enabled:
                rail_a = await self._compute_rail_a_status()
                if self.settings.rail_a_enabled and not rail_a.active:
                    decision = EntryDecision(
                        allowed=False,
                        reason=BUDGET_UNAVAILABLE_REASON,
                        rail="A-prospective",
                        details={
                            **loss_details,
                            "rail_a_inactive_reason": rail_a.reason,
                            "trade_date": rail_a.trade_date,
                        },
                    )
                    await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                    return decision
                if rail_a.active and (rail_a.halted or rail_a.flatten):
                    self._session_halted = True
                if self._session_halted:
                    decision = EntryDecision(
                        allowed=False,
                        reason=TIER1_HALT_REASON,
                        rail="A",
                        details=loss_details,
                    )
                    await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                    return decision
                if rail_a.active:
                    open_planned_loss = 0.0
                    unprotected_trade_ids: list[str] = []
                    unquantifiable_trade_ids: list[str] = []
                    for trade in live_open_trades:
                        planned_loss = planned_stop_loss_usd(
                            entry_price=trade.entry_price,
                            quantity=trade.quantity,
                            stop_price=trade.stop_price,
                        )
                        if planned_loss is None:
                            if trade.entry_price is not None and trade.entry_price > 0 and trade.quantity > 0:
                                planned_loss = round(trade.entry_price * trade.quantity * 100, 2)
                                unprotected_trade_ids.append(trade.trade_id)
                            else:
                                unquantifiable_trade_ids.append(trade.trade_id)
                        open_planned_loss += planned_loss or 0.0
                    reserved_planned_loss = round(
                        sum(
                            reservation.planned_stop_loss_usd
                            for reservation in other_reservations.values()
                        ),
                        2,
                    )
                    realized_loss = max(-(rail_a.realized_live_pnl_usd or 0.0), 0.0)
                    loss_budget = abs(rail_a.halt_threshold_usd or 0.0)
                    prospective_total = round(
                        realized_loss + open_planned_loss + reserved_planned_loss + proposed_loss,
                        2,
                    )
                    loss_details.update(
                        {
                            "realized_loss_usd": round(realized_loss, 2),
                            "open_planned_stop_loss_usd": round(open_planned_loss, 2),
                            "pending_reserved_stop_loss_usd": reserved_planned_loss,
                            "prospective_total_loss_usd": prospective_total,
                            "daily_loss_budget_usd": round(loss_budget, 2),
                            "remaining_loss_headroom_usd": round(
                                loss_budget
                                - realized_loss
                                - open_planned_loss
                                - reserved_planned_loss,
                                2,
                            ),
                            "unprotected_full_premium_trade_ids": unprotected_trade_ids,
                            "unquantifiable_open_trade_ids": unquantifiable_trade_ids,
                            "trade_date": rail_a.trade_date,
                        }
                    )
                    if unquantifiable_trade_ids:
                        decision = EntryDecision(
                            allowed=False,
                            reason=OPEN_POSITION_RISK_UNAVAILABLE_REASON,
                            rail="A-prospective",
                            details=loss_details,
                        )
                        await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                        return decision
                    if prospective_total > loss_budget:
                        decision = EntryDecision(
                            allowed=False,
                            reason=PROSPECTIVE_LOSS_HEADROOM_REASON,
                            rail="A-prospective",
                            details=loss_details,
                        )
                        await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                        return decision

            try:
                await self.trade_state_repository.upsert_entry_risk_reservation(
                    EntryRiskReservation(
                        trade_id=trade_id,
                        deployment_id=deployment_id,
                        symbol=symbol,
                        cluster=cluster,
                        planned_stop_loss_usd=proposed_loss,
                        expires_at=now + SIZED_ENTRY_RESERVATION_TTL,
                    )
                )
            except Exception as exc:
                decision = EntryDecision(
                    allowed=False,
                    reason=SIZED_ENTRY_RESERVATION_FAILED_REASON,
                    rail="entry-risk",
                    details={**loss_details, "error": str(exc)},
                )
                await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
                return decision
            decision = EntryDecision(
                allowed=True,
                reason="approved",
                rail="sized-entry",
                details=loss_details,
            )
            await self._emit_sized_entry_decision(deployment_id, trade_id, decision)
            return decision

    async def release_sized_entry(self, trade_id: str) -> None:
        async with self._sized_entry_lock:
            await self.trade_state_repository.mark_entry_risk_reservation_status(
                trade_id,
                "released",
            )

    async def commit_sized_entry(self, trade_id: str) -> None:
        """Release the transient reservation after durable open-trade truth exists."""
        async with self._sized_entry_lock:
            await self.trade_state_repository.mark_entry_risk_reservation_status(
                trade_id,
                "committed",
            )

    async def _emit_sized_entry_decision(
        self,
        deployment_id: str,
        trade_id: str,
        decision: EntryDecision,
    ) -> None:
        await self.event_repository.append(
            "risk_manager_sized_entry_decision",
            {
                "deployment_id": deployment_id,
                "trade_id": trade_id,
                "decision": "allowed" if decision.allowed else "blocked",
                "reason": decision.reason,
                "rail": decision.rail,
                "details": decision.details,
            },
        )

    async def _emit_entry_decision(self, deployment_id: str, decision: EntryDecision) -> None:
        await self.event_repository.append(
            "risk_manager_decision",
            {
                "rail": decision.rail,
                "deployment_id": deployment_id,
                "decision": "allowed" if decision.allowed else "blocked",
                "reason": decision.reason,
                "details": decision.details,
            },
        )

    async def _notify(self, *, title: str, body: str, level: str) -> None:
        if self.alert_mode == "off":
            return
        try:
            send_lathi_alert(
                title=title,
                body=body,
                level=level,
                mode=self.alert_mode,
                profile=self.alert_profile,
            )
        except Exception as exc:
            await self.event_repository.append(
                "risk_manager_notify_failed",
                {"title": title, "error": str(exc)},
            )
