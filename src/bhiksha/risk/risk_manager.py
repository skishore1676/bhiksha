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

RAIL A (two-tier portfolio daily-drawdown cap, realized-only v1):
  - tier 1 (halt): today's realized LIVE P&L <= -(max_daily_drawdown_pct/100)
    * usable_budget -> block all new entries this session.
  - tier 2 (flatten): <= -(flatten_daily_drawdown_pct/100) * usable_budget ->
    ALSO flatten open live positions via the caller's existing
    halt_and_flatten_positions/EmergencyBiasControl seam.
  - Missing data is fail-safe for trading continuity, not fail-open on a
    computed breach: no cash_budget_days row for today -> INACTIVE + one
    warning event, never a spurious halt. A P&L query failure is the same:
    INACTIVE + warning. But once both budget and P&L are available, a
    computed breach always acts -- there is no "fail open" path once data
    exists.

RAIL B (per-deployment auto-demote, one-way):
  - over the rolling last N (demote_window, default 10) CLOSED LIVE trades
    of a deployment, once at least min_n (default 10) trades exist, if mean
    P&L per trade < demote_threshold_usd (default $0) -> demote: block
    further live entries for it this session (consult-point decision) AND
    persist a local override (``DemotionStore``) the active-plan compiler
    merges at next compile to force the row to shadow.
  - one-way: DemotionStore.record_demotion is a no-op once a deployment is
    already recorded, so this never flaps and never auto-re-promotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.alerts import AlertMode, send_lathi_alert
from bhiksha.persistence.repository import CashBudgetRepository, EventRepository, TradeStateRepository
from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.risk.risk_settings import RiskSettings

SHADOW_ENTRY_ORDER_ID = "SHADOW_ENTRY"

TIER1_HALT_REASON = "risk_rail_a_tier1_halt"
TIER2_FLATTEN_REASON = "risk_rail_a_tier2_flatten"
RAIL_B_DEMOTED_REASON = "risk_rail_b_demoted"

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
    return trade.entry_order_id != SHADOW_ENTRY_ORDER_ID


def _is_closed_trade(trade: TradeRecord) -> bool:
    return trade.status == "closed"


def _realized_pnl_usd(trade: TradeRecord) -> float | None:
    if trade.entry_price is None or trade.exit_price is None:
        return None
    quantity = trade.exit_filled_quantity if trade.exit_filled_quantity is not None else trade.quantity
    if not quantity:
        return None
    return round((trade.exit_price - trade.entry_price) * quantity * 100, 2)


def _trade_date_et(timestamp: datetime) -> str:
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(et).date().isoformat()


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
        alert_mode: AlertMode = "live",
        alert_profile: str | None = None,
        now_fn=None,
    ) -> None:
        self.settings = settings
        self.cash_budget_repository = cash_budget_repository
        self.trade_state_repository = trade_state_repository
        self.event_repository = event_repository
        self.demotion_store = demotion_store or DemotionStore()
        self.alert_mode = alert_mode
        self.alert_profile = alert_profile
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
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
        total = 0.0
        for trade in trades:
            if not _is_live_trade(trade) or not _is_closed_trade(trade):
                continue
            if trade.exit_filled_at is None:
                continue
            if _trade_date_et(trade.exit_filled_at) != trade_date:
                continue
            pnl = _realized_pnl_usd(trade)
            if pnl is not None:
                total += pnl
        return round(total, 2)

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

        return BookActionsResult(
            rail_a=status,
            should_flatten=self._session_flattened,
            flatten_reason=TIER2_FLATTEN_REASON if self._session_flattened else None,
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
        # get_recent_trades is ordered by updated_at DESC; take the most
        # recent demote_window trades as "the rolling last N".
        window = deployment_live_closed[: self.settings.demote_window]
        if len(window) < self.settings.demote_min_n:
            return RailBStatus(demoted=False, reason="insufficient_trade_count", window_n=len(window))

        pnls = [pnl for trade in window if (pnl := _realized_pnl_usd(trade)) is not None]
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
            trade_ids=[trade.trade_id for trade in window],
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
                f"To re-promote: an operator must delete the '{deployment_id}' entry from "
                f"{self.demotion_store.path} (one-way -- there is no auto-re-promote)."
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
