"""Execution supervision for planned trades."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import UTC
import math
import os
import uuid
from typing import Any

from loguru import logger

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.config.models import AppConfig
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import ExitMode, SignalDirection
from bhiksha.domain.models import ExitDecision, ExitPlan, PartialFillRecord, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.execution.order_manager import OrderResult, normalize_option_symbol, round_price
from bhiksha.execution.pricing import scale_spread_fraction, select_entry_limit
from bhiksha.execution.profile_exit import (
    ProfileExitFields,
    ProfileExitState,
    ProfileMarketView,
    profile_decision_to_exit_decision,
    profile_exit_dispatch_allowed,
)
from bhiksha.execution.profile_exit_shadow import evaluate_and_record_profile_exit, ProfileExitDispatchError
from bhiksha.integrations.manual_sheet_status import ManualSheetStatusWriter
from bhiksha.market_data.session import as_et_time
from bhiksha.persistence.repository import EventRepository, NullEventRepository, NullTradeStateRepository, TradeStateRepository
from bhiksha.state.lifecycle import LifecycleTransition, TradeLifecycleStore
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.time_utils import parse_time_text

ENTRY_CANCEL_STATUS_READBACK_TIMEOUT_SECONDS = 1.0

# Audit fixes A.1/A.2 (2026-07-08): broker statuses that prove a canceled exit
# order is DEAD (cannot fill any further). FILLED is handled separately as the
# full-fill race. Anything else -- None/error/NEW/SUBMITTED/PARTIALLY_FILLED/
# unknown -- means the order may still fill, so a reprice must not resubmit on
# top of it without a clean cancel confirmation.
_EXIT_ORDER_DEAD_STATUSES = frozenset({"CANCELED", "REJECTED", "EXPIRED"})

# Audit fix 3 (2026-07-08): give-up ceiling for the partial-fill enrichment
# sweep. One attempt is counted per unresolved poll (error/timeout/non-terminal
# status); at the reconciliation cadence (~15s) 40 attempts is ~10 minutes of
# retrying before the row is marked abandoned with a reason instead of being
# re-polled forever against a degraded broker.
_PARTIAL_FILL_ENRICH_MAX_ATTEMPTS = 40


@dataclass(frozen=True, slots=True)
class _EntryWaitResult:
    plan: TradePlan
    filled: bool
    payload: dict | None
    error: str | None
    cancelled_without_fill: bool = False


@dataclass(frozen=True, slots=True)
class _EntryRepriceResult:
    plan: TradePlan
    error: str | None = None
    filled: bool = False
    payload: dict | None = None
    cancelled_without_fill: bool = False


@dataclass(frozen=True, slots=True)
class _EntryCancelResult:
    cancel_ok: bool
    cancel_error: str | None
    status: str | None = None
    payload: dict | None = None
    status_error: str | None = None

    @property
    def filled(self) -> bool:
        return str(self.status or "").upper() == "FILLED"


@dataclass(frozen=True, slots=True)
class _ExitCancelRaceOutcome:
    """Verdict of ``_resolve_exit_cancel_for_reprice`` (audit fixes A.1/A.2).

    * ``finalized`` -- the old order already filled the whole position; the
      fill truth was recorded and ``plan`` carries the close. NO resubmit.
    * ``resubmit`` -- safe to place the replacement order; ``position`` is the
      position to resubmit with (quantity reduced to the residual when a
      partial fill was detected and recorded).
    * ``blocked`` -- the cancel is unconfirmed AND the readback could not prove
      the order dead: resubmitting could double-sell, so the caller must skip
      this cycle and let the pending-exit poller retry.
    """

    action: str
    position: TrackedPosition | None = None
    plan: ExitPlan | None = None
    cancel_error: str | None = None


class ExecutionSupervisor:
    """Coordinates planning and event logging for a signal."""

    def __init__(
        self,
        planner: ExecutionPlanner | None = None,
        event_repository: EventRepository | None = None,
        app_config: AppConfig | None = None,
        lifecycle_store: TradeLifecycleStore | None = None,
        event_bus: InMemoryEventBus | None = None,
        trade_state_repository: TradeStateRepository | None = None,
        manual_status_writer: ManualSheetStatusWriter | None = None,
        reconcile_trigger: asyncio.Event | None = None,
        exit_edge_recorder: Any | None = None,
    ) -> None:
        self.planner = planner or ExecutionPlanner()
        self.event_repository = event_repository or NullEventRepository()
        self.trade_state_repository = trade_state_repository or NullTradeStateRepository()
        self.app_config = app_config or AppConfig()
        self.lifecycle_store = lifecycle_store or TradeLifecycleStore()
        self.event_bus = event_bus
        self.manual_status_writer = manual_status_writer
        self.reconcile_trigger = reconcile_trigger
        self.exit_edge_recorder = exit_edge_recorder
        self._entry_lock = asyncio.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._disabled_entry_deployments: set[str] = set()
        # H3: per-position profile-exit ladder state (peak premium, T1-banked,
        # banked_quantity, breakeven_emitted). Keyed by a stable position id so it
        # survives across monitor ticks; the supervisor is the lifecycle owner and
        # clears it on close. Without this the ladder would reset every tick and
        # re-bank partials / re-arm giveback / re-emit breakeven.
        self._profile_exit_states: dict[str, ProfileExitState] = {}

    async def close(self) -> None:
        if self.exit_edge_recorder is not None:
            self.exit_edge_recorder.close()
        await self.planner.close()

    # ------------------------------------------------------------------ #
    # H3: profile-exit ladder-state lifecycle (per-position, supervisor-owned)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _profile_state_key(position: TrackedPosition) -> str:
        """Stable per-position key for the profile-exit ladder state.

        Prefers the trade id (the position's true identity across ticks); falls
        back to the entry order id, then a (deployment, symbol, option) tuple so
        the key is well-defined even for reconstructed positions.
        """
        identity = position.trade_id or position.order_id
        if identity:
            return f"trade:{identity}"
        return f"pos:{position.deployment_id}:{position.symbol}:{position.option_symbol}"

    def get_or_create_profile_exit_state(
        self,
        position: TrackedPosition,
        *,
        entry_premium: float,
    ) -> ProfileExitState:
        """Return the persisted ladder state for a position, creating it once.

        The state is created the first time a position is evaluated (seeded with
        the entry premium as the initial peak) and then REUSED across every
        subsequent monitor tick, so banked partials, the giveback high-water mark
        and the breakeven-emitted flag all persist.
        """
        key = self._profile_state_key(position)
        state = self._profile_exit_states.get(key)
        if state is not None and _profile_state_identity_mismatch(
            state, entry_premium=entry_premium
        ):
            # Identity backstop (audit fix 2026-07-02): the cached ladder was
            # seeded by a DIFFERENT fill (trade-identity mismatch upstream in
            # reconciliation matching). Driving exits off another fill's peak /
            # banked partials produced spurious full square-offs in the audit
            # repro. Reseed clean rather than inherit; the mismatch is logged
            # for the audit trail.
            logger.warning(
                "profile_exit_state_identity_mismatch key={} cached_seed={} current_entry={} "
                "banked_quantity={} position_quantity={} -- reseeding ladder",
                key,
                state.seed_entry_premium,
                entry_premium,
                state.banked_quantity,
                position.quantity,
            )
            state = None
        if state is None:
            state = ProfileExitState.new(entry_premium, seed_quantity=position.quantity)
            self._profile_exit_states[key] = state
        return state

    def clear_profile_exit_state(self, position: TrackedPosition) -> None:
        """Drop a position's persisted ladder state (call on close/flatten)."""
        self._profile_exit_states.pop(self._profile_state_key(position), None)

    def _tracked_position_like(self, position: TrackedPosition) -> TrackedPosition | None:
        """Return the CURRENT tracked position matching ``position``'s identity.

        After the armed profile route applies a partial scale or a stop move via the
        locked handlers, the tracker holds the updated (residual / re-stopped)
        position while the caller's local ``position`` is stale. This re-reads the
        tracker so the managed-position return reflects the route's effect. Matches
        on (deployment_id, symbol, option_symbol) — the within-deployment position
        identity — and returns ``None`` when the tracker no longer lists it (e.g. a
        full close already removed it).
        """
        for tracked in self.planner.position_tracker.active_positions():
            if (
                tracked.deployment_id == position.deployment_id
                and tracked.symbol == position.symbol
                and tracked.option_symbol == position.option_symbol
            ):
                return tracked
        return None

    def _clear_profile_exit_state_for_identity(
        self,
        *,
        trade_id: str | None,
        order_id: str | None,
        deployment_id: str,
        symbol: str,
        option_symbol: str | None,
    ) -> None:
        """Clear ladder state on a terminal close that only has plan-level identity.

        NEW-4: some terminal close paths (e.g. pre-fill entry cancels) operate on a
        ``TradePlan``, not a ``TrackedPosition``. Clear by the same key derivation so
        no ladder state can leak past a terminal close on any path.
        """
        identity = trade_id or order_id
        if identity:
            self._profile_exit_states.pop(f"trade:{identity}", None)
        self._profile_exit_states.pop(f"pos:{deployment_id}:{symbol}:{option_symbol}", None)

    # ------------------------------------------------------------------ #
    # Profile-exit SHADOW-RECORD dual-run (record-only this wave; OFF flag)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _deployment_carries_exit_profile(deployment: DeploymentManifest) -> bool:
        """True when a deployment pins a v2 operator exit profile."""
        exit_spec = getattr(deployment, "exit", None)
        return bool(getattr(exit_spec, "profile_exit_id", None))

    @staticmethod
    def _profile_exit_drives_live(deployment: DeploymentManifest) -> bool:
        """Operator live-enablement flag for the profile-exit evaluator.

        DEFAULT FALSE and the ONLY state this wave ships. This is the SINGLE flip
        seam: when it returns True a recorded profile decision becomes eligible to
        DRIVE a real exit (still subject to the fail-closed dispatch allowlist in
        ``profile_exit_dispatch_allowed``); when False the profile decision is
        record-only and can never reach the broker/order path.

        Resolves from the deployment ``exit.profile_exit_drives_live`` flag, OR an
        env override ``BHIKSHA_PROFILE_EXIT_LIVE`` (``1``/``true``/``yes``/``on``).
        Both default to OFF — this wave never enables it.
        """
        exit_spec = getattr(deployment, "exit", None)
        if bool(getattr(exit_spec, "profile_exit_drives_live", False)):
            return True
        env = os.environ.get("BHIKSHA_PROFILE_EXIT_LIVE")
        if env is not None and env.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return False

    @staticmethod
    def _resolved_runtime_mode(deployment: DeploymentManifest) -> str | None:
        """The deployment's ACTUAL runtime mode for the profile-exit dispatch gate.

        HIGH-1: the gate must consult the deployment's real runtime mode, NOT a
        hardcoded ``live_approval_gated`` literal. The real source is the
        deployment's execution config (``execution.runtime_mode`` — the kernel
        ``RuntimeMode`` wire value the deployment was compiled/declared with).

        FAILS CLOSED by construction: returns whatever the deployment actually
        declares, or ``None`` when it declares nothing. The downstream fail-closed
        allowlist (``profile_exit_dispatch_allowed``) opens ONLY for the exact
        string ``live_approval_gated``; ``None`` and every other value
        (``live_automated``/``shadow``/``advisory``/unknown) keep the gate shut.
        This method never substitutes a permissive default for a missing/unknown
        mode — a deployment that does not provably run ``live_approval_gated``
        cannot dispatch a profile exit.
        """
        execution_spec = getattr(deployment, "execution", None)
        mode = getattr(execution_spec, "runtime_mode", None)
        if mode is None:
            return None
        # Normalize a kernel ``RuntimeMode`` enum (or any object) to its wire
        # string; the allowlist compares against canonical strings. A non-string,
        # non-enum value normalizes to its ``str(...)`` form, which will simply
        # miss the allowlist and fail closed.
        normalized = getattr(mode, "value", mode)
        if not isinstance(normalized, str):
            normalized = str(normalized)
        return normalized

    def _profile_exit_is_authoritative(
        self, deployment: DeploymentManifest, position: TrackedPosition
    ) -> bool:
        """Is the PROFILE-EXIT route the sole exit authority for this position?

        THE DOUBLE-EXIT / AUTHORITY INVARIANT (the #1 risk), evaluated STATELESSLY
        from the SAME fail-closed dispatch gate the armed profile route uses. Returns
        True iff the deployment carries an exit profile AND
        ``profile_exit_dispatch_allowed`` would open for this (deployment, position):
        the operator flag is ON, the deployment's REAL runtime mode is
        ``live_approval_gated``, it is not shadow-only, and the position source is an
        explicit live entry. In that and only that state the profile route owns the
        position's exit and the native exit path (``handle_exit``) YIELDS.

        Why stateless (not a mutable claim set): the native exit task runs with a
        STALE pre-manage position snapshot, but the only gate input it reads from the
        position is ``source``, which is SNAPSHOT-CONSISTENT within a tick: both the
        manage path and the trailing native exit task read positions from the SAME
        reconciliation snapshot, so they see the same ``source`` value.
        (``source`` is NOT immutable across reconcile sweeps — since 2026-07-02 a
        sweep may relabel a matched open live trade's position ``live_open`` — but
        a sweep swaps the whole snapshot between ticks, never inside one.)
        ``deployment`` is the same object. So this predicate
        returns the SAME verdict whether evaluated inside ``manage`` (where the
        profile route acts) or in the trailing native ``exit`` task (where it yields)
        — they are guaranteed consistent, with no lifecycle/leak/ordering hazard.

        With the operator flag OFF (its default and the only state shipped) this is
        ALWAYS False, so the native path is the sole authority and production
        behavior is unchanged.
        """
        if not self._deployment_carries_exit_profile(deployment):
            return False
        drives_live = self._profile_exit_drives_live(deployment)
        return profile_exit_dispatch_allowed(
            live=drives_live,
            deployment_shadow_only=not drives_live,
            position_source=position.source,
            runtime_mode=self._resolved_runtime_mode(deployment),
        )

    async def _record_profile_exit_shadow(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        quote: Any,
        *,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> TrackedPosition | None:
        """RECORD (always) + DISPATCH-WHEN-GATED the profile-exit decision (PART A).

        Runs the operator's exit profile against the live option quote ALREADY
        fetched this tick and RECORDS the decision to the shadow event sink (a
        ``profile_exit_shadow`` event). State persists across ticks via the
        supervisor-owned ``ProfileExitState`` store and is cleared on close.

        Returns the position to treat as the managed position after this tick:
          * the (possibly stop-adjusted) ``position`` when nothing was dispatched
            or only a stop move was applied;
          * ``None`` when the armed route fully closed the position this tick.

        DEFAULT (operator flag OFF) — production behavior, unchanged:
        ``profile_exit_drives_live`` is OFF (its default and the only state
        shipped) so the recorder is told ``live=False``; the fail-closed dispatch
        gate (``profile_exit_dispatch_allowed``) cannot open and
        ``outcome.dispatched`` is always False. The profile decision is RECORDED
        ONLY and NEVER routed into the exit/order path; this method places no orders
        and returns ``position`` unchanged. The native exit path remains the SOLE
        authority (``_profile_exit_is_authoritative`` is always False).

        ARMED route (Phase 2, dormant by default) — only when the gate is OPEN:
        the mapped domain ``ExitDecision`` (``outcome.exit_decision``) is dispatched
        through the EXISTING ``_handle_exit_locked`` dispatcher (NOT a new order
        path), so a profile exit inherits the SAME locking, idempotency, dry_run
        and order-placement safety as a native exit. dry_run is respected
        end-to-end (a dry-run square_off books a paper close, places NO real order).

        DOUBLE-EXIT / AUTHORITY INVARIANT (the #1 risk): this method runs at the
        END of ``_manage_open_position_locked`` — INSIDE ``self._symbol_locks
        [symbol]`` and, per the serial per-symbol execution dispatcher, BEFORE the
        same tick's native ``exit`` task runs. When the gate is OPEN for a position
        the profile route is its SOLE exit authority; the native ``handle_exit``
        consults the SAME fail-closed gate via ``_profile_exit_is_authoritative`` and
        YIELDS for that position. The verdict is computed STATELESSLY (it depends only
        on the deployment and ``position.source``, snapshot-consistent within the tick), so it is identical
        whether evaluated here or in the trailing native task that carries a stale
        pre-route snapshot — the two authorities can NEVER act on the same position
        conflictingly (no double close, no fighting stops), with no claim-set
        lifecycle to leak or race. The route reaches the dispatcher via
        ``_handle_exit_locked`` (lock already held), never ``handle_exit``, so it is
        never blocked by its own guard.
        """
        if not self._deployment_carries_exit_profile(deployment):
            return position
        if position.option_symbol is None or position.quantity <= 0 or position.entry_price is None:
            return position
        if quote is None:
            return position

        now = now or datetime.now(UTC)
        fields = ProfileExitFields.from_exit_spec(deployment.exit)
        market = ProfileMarketView(
            current_premium=quote.exit_reference_price,
            # Wall-clock ET time-of-day for the EOD rung (best-effort; the
            # supervisor's own close_due_positions sweep remains the EOD authority).
            bar_time_et=as_et_time(now),
            bid=getattr(quote, "bid", None),
            ask=getattr(quote, "ask", None),
            last=getattr(quote, "last", None),
        )
        state = self.get_or_create_profile_exit_state(position, entry_premium=position.entry_price)

        # FLIP SEAM (the operator's one-line live enablement is the flag inside
        # ``_profile_exit_drives_live``; everything downstream of ``live`` is
        # already wired). This wave: ``drives_live`` is False, so ``live=False``.
        drives_live = self._profile_exit_drives_live(deployment)

        # MEDIUM-1(flip): protect a mid-position flag flip. With the flag OFF the
        # recorder still advances ``state`` every tick (peak ratchets; a T1 touch
        # sets target_1_banked/banked_quantity/breakeven_emitted) but places
        # nothing. If the operator flips ``profile_exit_drives_live`` ON while a
        # position is open, the now-live evaluator would inherit that shadow-
        # advanced ladder and UNDER-SIZE the live exit (treating a never-placed
        # partial as banked) and SKIP the breakeven. Guard the transition:
        #   * shadow tick (drives_live False): mark the state shadow-advanced.
        #   * first live tick on a shadow-advanced state: RESEED the ladder fresh
        #     from the current premium before the live evaluation, so the live
        #     evaluator sees the position as if opening clean.
        # A position that has only ever run live never sets ``shadow_advanced``,
        # so a clean-from-entry live ladder is untouched (no reseed).
        if not drives_live:
            state.mark_shadow_advanced()
        elif state.shadow_advanced:
            state.reseed_for_live(position.entry_price, seed_quantity=position.quantity)

        outcome = await evaluate_and_record_profile_exit(
            event_sink=self.event_repository,
            fields=fields,
            deployment_id=deployment.deployment_id,
            symbol=position.symbol,
            option_symbol=position.option_symbol,
            entry_premium=position.entry_price,
            quantity=position.quantity,
            market=market,
            entry_time=position.entry_timestamp,
            state=state,
            # --- DISPATCH GATE INPUTS ---
            # ``live`` is THE operator flip. OFF this wave => the fail-closed
            # allowlist returns False => the decision is recorded, never dispatched.
            live=drives_live,
            # ``deployment_shadow_only`` is the SINGLE switch's shadow precondition:
            # it is True (shadow) exactly when the drive flag is OFF, so flipping
            # ``profile_exit_drives_live`` is sufficient to satisfy THIS precondition
            # (the gate still independently requires runtime_mode + a live source).
            deployment_shadow_only=not drives_live,
            position_source=position.source,
            # HIGH-1: the deployment's ACTUAL runtime mode (never a hardcoded
            # literal). Resolved from the real execution config; ``None`` when the
            # deployment does not declare one. The fail-closed dispatch allowlist
            # opens ONLY for ``live_approval_gated`` — so a deployment running
            # ``live_automated`` (or shadow/advisory/unknown/None) keeps the gate
            # SHUT and can never dispatch a profile exit, matching the rest of
            # Bhiksha. Going live therefore requires BOTH the operator flag flip
            # AND a deployment whose real mode is ``live_approval_gated``.
            runtime_mode=self._resolved_runtime_mode(deployment),
            now=now,
            # The supervisor's EOD authority is close_due_positions; do not hard-fail
            # the shadow record when a bar clock is unavailable.
            require_bar_time_for_eod=False,
        )

        # ARMED DISPATCH ROUTE. ``outcome.dispatched`` is True ONLY when the
        # fail-closed gate is OPEN for this position AND the profile decision would
        # act (an exit, or a STOP_TO_BREAKEVEN stop move). With the operator flag OFF
        # (its default and the only state shipped) the gate cannot open, so this is
        # ALWAYS False and we return the (unchanged) position — the DORMANT default,
        # production behavior untouched. When armed, route the ALREADY-mapped domain
        # ``ExitDecision`` through the EXISTING ``_handle_exit_locked`` dispatcher
        # (NOT a new order path; we are inside ``self._symbol_locks[symbol]`` here, so
        # the public lock-acquiring ``handle_exit`` would deadlock on the non-reentrant
        # lock — hence the locked entry directly, mirroring the existing shadow-stop
        # path in this same method). Per-fsm_action the dispatcher already routes
        # correctly:
        #   * STOP_TO_BREAKEVEN -> action="hold" + replacement_stop_price -> the
        #     ``_apply_replacement_stop`` branch tightens the stop (NOT an exit order).
        #   * PARTIAL_SCALE     -> action="square_off" + features[exit_quantity]/
        #     [partial_scale] -> ``_handle_partial_scale_locked`` closes only that
        #     quantity and re-arms the residual stop.
        #   * SQUARE_OFF/HARD_FLAT -> action="square_off" full close.
        # dry_run is threaded through unchanged, so a dry-run dispatch books a paper
        # exit and places NO real order. An audit event records the routed dispatch.
        #
        # DOUBLE-EXIT SAFETY: the native exit path is held off STATELESSLY by
        # ``_profile_exit_is_authoritative`` (consulted in ``handle_exit``), which
        # opens on exactly the same fail-closed gate as this route — so whenever this
        # route can act, the same tick's native exit task yields. No conflicting
        # double action is possible. (The profile route reaches the dispatcher via
        # ``_handle_exit_locked``, never ``handle_exit``, so it is never blocked by
        # that guard.)
        if outcome.dispatched and outcome.exit_decision is not None:
            await self.event_repository.append(
                "profile_exit_dispatch_routed",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "trade_id": position.trade_id,
                    "rule": outcome.decision.rule.value,
                    "fsm_action": outcome.decision.fsm_action.value,
                    "action": outcome.exit_decision.action,
                    "dry_run": dry_run,
                },
            )
            # Route through the EXISTING locked dispatcher (lock already held here).
            # An ARMED dispatch is a REAL exit action: unlike the benign shadow
            # RECORD above, a failure here may leave the position unprotected (the
            # dispatcher can cancel the resting stop before placing the close). So we
            # must NOT let the broad PART A "never break management" guard swallow it
            # as a shadow error and return the stale position as managed — that is the
            # silent-naked footgun. Surface it as a protective_stop_failure
            # runtime_issue and re-raise (ProfileExitDispatchError) so it propagates
            # exactly like a native exit failure: loud, never false-managed. Dormant
            # with the flag OFF (the gate never opens, so this never runs).
            try:
                plan = await self._handle_exit_locked(
                    deployment, position, outcome.exit_decision, dry_run=dry_run
                )
            except Exception as exc:
                await self.event_repository.append(
                    "runtime_issue",
                    {
                        "category": "protective_stop_failure",
                        "source": "profile_exit_armed_dispatch",
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "trade_id": position.trade_id,
                        "fsm_action": outcome.decision.fsm_action.value,
                        "dry_run": dry_run,
                        "error": str(exc),
                    },
                )
                raise ProfileExitDispatchError(
                    f"armed profile-exit dispatch failed for {deployment.deployment_id}"
                    f"/{position.option_symbol}: {exc}"
                ) from exc
            # A full square_off/hard_flat returns a terminal ExitPlan (paper close in
            # dry_run, live submission otherwise) AND closes the tracker/ladder state
            # -> report the position as closed for this tick. A partial scale also
            # returns an ExitPlan but leaves the residual runner OPEN (re-armed). A
            # hold-class STOP_TO_BREAKEVEN returns None (no ExitPlan) but moved the
            # stop. Decide closed-vs-open from the ACTION (not merely the plan) so a
            # partial is never mistaken for a full close.
            if (
                plan is not None
                and outcome.exit_decision.action == "square_off"
                and not _decision_is_partial_scale(outcome.exit_decision)
            ):
                return None
            # Partial or stop-move: the position remains open. Re-read the tracked
            # position so the managed-position return reflects the residual/stop move
            # the locked handlers persisted; fall back to the input position.
            return self._tracked_position_like(position) or position

        if position.source == "shadow" and dry_run and outcome.decision.exit:
            shadow_decision = profile_decision_to_exit_decision(
                outcome.decision,
                deployment_id=deployment.deployment_id,
                symbol=position.symbol,
                timestamp=now,
            )
            await self.event_repository.append(
                "profile_exit_shadow_routed",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "trade_id": position.trade_id,
                    "rule": outcome.decision.rule.value,
                    "fsm_action": outcome.decision.fsm_action.value,
                    "action": shadow_decision.action,
                    "dry_run": True,
                },
            )
            plan = await self._handle_exit_locked(deployment, position, shadow_decision, dry_run=True)
            if (
                plan is not None
                and shadow_decision.action == "square_off"
                and not _decision_is_partial_scale(shadow_decision)
            ):
                return None
            return self._tracked_position_like(position) or position

        return position

    async def handle_signal(
        self,
        deployment: DeploymentManifest,
        decision: SignalDecision,
        *,
        dry_run: bool,
        simulate_only: bool = False,
        live_entry_block_reason: str | None = None,
    ) -> TradePlan | None:
        await self.event_repository.append(
            "signal_decision",
            {
                "deployment_id": decision.deployment_id,
                "symbol": decision.symbol,
                "timestamp": decision.timestamp.isoformat(),
                "signal": decision.signal,
                "direction": decision.direction.value if decision.direction else None,
                "reason": decision.reason,
                "features": decision.features,
            },
        )
        if not self.can_submit_deployment_entry(deployment):
            return None
        if decision.signal:
            if _is_self_disarming_manual_deployment(deployment):
                self._disabled_entry_deployments.add(deployment.deployment_id)
            await self._record_manual_status(
                deployment,
                stage="signal_triggered",
                writer_call=self.manual_status_writer.mark_signal_triggered(deployment, decision)
                if self.manual_status_writer is not None
                else None,
            )
        if self.event_bus is not None:
            await self.event_bus.publish(SignalEvaluatedEvent(decision=decision))
        async with self._entry_lock:
            lifecycle = self.lifecycle_store.get(deployment.symbol, deployment.deployment_id)
            if not self.lifecycle_store.can_submit_entry(deployment.symbol, deployment.deployment_id):
                await self.event_repository.append(
                    "lifecycle_entry_blocked",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": deployment.symbol,
                        "state": lifecycle.state.value if lifecycle else None,
                    },
                )
                await self._record_manual_status(
                    deployment,
                    stage="entry_blocked",
                    writer_call=self.manual_status_writer.mark_entry_blocked(
                        deployment,
                        event_at=decision.timestamp,
                        note=f"lifecycle_blocked:{lifecycle.state.value if lifecycle else 'unknown'}",
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
                return None
            if decision.signal and decision.direction is not None and live_entry_block_reason and not dry_run and not simulate_only:
                plan = TradePlan(
                    trade_id=str(uuid.uuid4()),
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol="",
                    quantity=0,
                    estimated_entry_price=0.0,
                    risk_reasons=[live_entry_block_reason],
                    dry_run=False,
                    order_id=None,
                    underlying_entry_price=_underlying_entry_price(decision),
                    entry_timestamp=decision.timestamp,
                )
            else:
                plan = await self.planner.plan_entry(
                    deployment,
                    decision,
                    dry_run=dry_run,
                    simulate_only=simulate_only,
                )
            if plan is not None:
                if (
                    _entry_plan_approved(plan)
                    and plan.quantity > 0
                    and plan.option_symbol
                    and (plan.order_id is not None or plan.dry_run)
                ):
                    mode = "live" if plan.order_id and not plan.dry_run else ("shadow" if simulate_only else "dry_run")
                    await self._record_manual_status(
                        deployment,
                        stage="entry_planned",
                        writer_call=self.manual_status_writer.mark_entry_planned(
                            deployment,
                            plan=plan,
                            mode=mode,
                        )
                        if self.manual_status_writer is not None
                        else None,
                    )
                else:
                    note = ",".join(plan.risk_reasons) or "entry_blocked"
                    await self._record_manual_status(
                        deployment,
                        stage="entry_blocked",
                        writer_call=self.manual_status_writer.mark_entry_blocked(
                            deployment,
                            event_at=plan.entry_timestamp or decision.timestamp,
                            note=note,
                            trade_id=plan.trade_id,
                        )
                        if self.manual_status_writer is not None
                        else None,
                    )
            if plan is not None:
                if plan.order_id:
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="pending_entry",
                            entry_order_id=plan.order_id,
                            can_ladder=plan.quantity >= 2,
                        )
                    )
                    transition = self.lifecycle_store.begin_entry(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id=plan.order_id,
                    )
                    await self._emit_lifecycle_transition(transition, reason="entry_submitted")
                if (
                    simulate_only
                    and _entry_plan_approved(plan)
                    and plan.quantity > 0
                    and plan.option_symbol
                    and plan.order_id is None
                ):
                    self.planner.position_tracker.open_position(
                        deployment.symbol,
                        deployment.deployment_id,
                        trade_id=plan.trade_id,
                        option_symbol=plan.option_symbol,
                        quantity=plan.quantity,
                        entry_price=plan.estimated_entry_price,
                        underlying_entry_price=plan.underlying_entry_price,
                        entry_timestamp=plan.entry_timestamp,
                        source="shadow",
                        order_id="SHADOW_ENTRY",
                    )
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="open_unprotected",
                            entry_order_id="SHADOW_ENTRY",
                            can_ladder=plan.quantity >= 2,
                        )
                    )
                    transition = self.lifecycle_store.mark_open(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id="SHADOW_ENTRY",
                        protected=False,
                    )
                    await self._emit_lifecycle_transition(transition, reason="shadow_entry_open")
                    await self.event_repository.append(
                        "shadow_entry_assumed",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": deployment.symbol,
                            "trade_id": plan.trade_id,
                            "option_symbol": plan.option_symbol,
                            "quantity": plan.quantity,
                            "entry_price": plan.estimated_entry_price,
                            "underlying_entry_price": plan.underlying_entry_price,
                            "entry_timestamp": plan.entry_timestamp.isoformat() if plan.entry_timestamp else None,
                            "risk_reasons": list(plan.risk_reasons),
                        },
                    )
                elif not dry_run and plan.order_id:
                    plan = await self._protect_live_entry(plan, deployment)
                elif dry_run and plan.order_id:
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="open_unprotected",
                            entry_order_id=plan.order_id,
                            can_ladder=plan.quantity >= 2,
                        )
                    )
                    transition = self.lifecycle_store.mark_open(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id=plan.order_id,
                        protected=False,
                    )
                    await self._emit_lifecycle_transition(transition, reason="dry_run_entry_open")
                await self.event_repository.append("trade_plan", asdict(plan))
            return plan

    def can_submit_deployment_entry(self, deployment: DeploymentManifest) -> bool:
        if not deployment.enabled:
            return False
        if _is_self_disarming_manual_deployment(deployment):
            return deployment.deployment_id not in self._disabled_entry_deployments
        return True

    async def _protect_live_entry(self, plan: TradePlan, deployment: DeploymentManifest) -> TradePlan:
        wait_result = await self._wait_for_entry_fill_or_cancel(plan, deployment)
        plan = wait_result.plan
        filled = wait_result.filled
        payload = wait_result.payload
        error = wait_result.error
        if not filled:
            if wait_result.cancelled_without_fill:
                await self._release_cash_guard_reservation(plan.trade_id)
                self.planner.position_tracker.close_position(
                    deployment.symbol,
                    deployment.deployment_id,
                    option_symbol=plan.option_symbol,
                    order_id=plan.order_id,
                )
                self._clear_profile_exit_state_for_identity(
                    trade_id=plan.trade_id,
                    order_id=plan.order_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    option_symbol=plan.option_symbol,
                )
                await self.trade_state_repository.mark_closed(plan.trade_id)
                transition = self.lifecycle_store.mark_closed(deployment.symbol, deployment.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="entry_reprice_no_fill_cancelled")
                return plan
            normalized_error = (error or "").upper()
            if normalized_error in {"REJECTED", "CANCELED", "EXPIRED"}:
                await self._release_cash_guard_reservation(plan.trade_id)
                self.planner.position_tracker.close_position(
                    deployment.symbol,
                    deployment.deployment_id,
                    option_symbol=plan.option_symbol,
                    order_id=plan.order_id,
                )
                self._clear_profile_exit_state_for_identity(
                    trade_id=plan.trade_id,
                    order_id=plan.order_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    option_symbol=plan.option_symbol,
                )
                await self.trade_state_repository.mark_closed(plan.trade_id)
                transition = self.lifecycle_store.mark_closed(deployment.symbol, deployment.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="entry_unfilled_closed")
                await self.event_repository.append(
                    "entry_reconcile_released",
                    {
                        "deployment_id": plan.deployment_id,
                        "trade_id": plan.trade_id,
                        "order_id": plan.order_id,
                        "status": normalized_error or "UNKNOWN",
                        "payload": payload or {},
                    },
                )
                return plan
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=plan.trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    option_symbol=plan.option_symbol,
                    quantity=plan.quantity,
                    entry_price=plan.estimated_entry_price,
                    underlying_entry_price=plan.underlying_entry_price,
                    entry_timestamp=plan.entry_timestamp,
                    status="pending_entry_reconcile",
                    entry_order_id=plan.order_id,
                    can_ladder=plan.quantity >= 2,
                )
            )
            transition = self.lifecycle_store.mark_reconciliation_hold(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=plan.order_id,
            )
            await self._emit_lifecycle_transition(transition, reason="entry_fill_timeout_reconcile")
            await self.event_repository.append(
                "entry_fill_timeout_reconcile",
                {
                    "deployment_id": plan.deployment_id,
                    "trade_id": plan.trade_id,
                    "order_id": plan.order_id,
                    "error": error,
                    "payload": payload or {},
                },
            )
            if self.reconcile_trigger is not None:
                self.reconcile_trigger.set()
            return plan

        await self._finalize_cash_guard_reservation(plan.trade_id)
        filled_entry_price = _filled_entry_price(payload, fallback=plan.estimated_entry_price)
        risk_details = {
            **dict(plan.risk_details),
            "submitted_entry_limit_price": plan.estimated_entry_price,
            "broker_average_fill_price": filled_entry_price,
        }
        plan = replace(plan, estimated_entry_price=filled_entry_price, risk_details=risk_details)
        # Freeze the observational cohort only from CONFIRMED broker fill
        # truth. This is a bounded queue write: no SQLite, await, replay, or
        # broker call occurs on the entry/money path. Failure only affects the
        # experiment's health/censor state.
        if self.exit_edge_recorder is not None:
            confirmed_price, confirmed_quantity, confirmed_at = _confirmed_entry_fill_facts(payload)
            self.exit_edge_recorder.try_register_entry(
                deployment=deployment,
                trade_id=plan.trade_id,
                option_symbol=plan.option_symbol,
                entry_timestamp=confirmed_at,
                entry_premium=confirmed_price,
                quantity=confirmed_quantity,
            )
        stop_result, stop_price, target_order_id, target_price = await self._arm_position_protection(
            deployment,
            option_symbol=plan.option_symbol,
            quantity=plan.quantity,
            entry_price=plan.estimated_entry_price,
            dry_run=False,
            event_payload={
                "deployment_id": plan.deployment_id,
                "entry_order_id": plan.order_id,
            },
            emit_suppressed_event=True,
        )
        protection_error = None if stop_result.order_id else (stop_result.error or "missing_stop_order_id")
        protection_status = "target_active" if target_order_id and stop_result.order_id else (
            "open_protected" if stop_result.order_id else "open_unprotected"
        )
        self.planner.position_tracker.open_position(
            deployment.symbol,
            deployment.deployment_id,
            trade_id=plan.trade_id,
            option_symbol=plan.option_symbol,
            quantity=plan.quantity,
            entry_price=plan.estimated_entry_price,
            underlying_entry_price=plan.underlying_entry_price,
            entry_timestamp=plan.entry_timestamp,
            source="live_open",
            order_id=plan.order_id,
            stop_order_id=stop_result.order_id,
            stop_price=stop_price,
            target_order_id=target_order_id,
            target_price=target_price,
        )
        await self._upsert_trade_record(
            TradeRecord(
                trade_id=plan.trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                option_symbol=plan.option_symbol,
                quantity=plan.quantity,
                entry_price=plan.estimated_entry_price,
                underlying_entry_price=plan.underlying_entry_price,
                entry_timestamp=plan.entry_timestamp,
                status=protection_status,
                entry_order_id=plan.order_id,
                stop_order_id=stop_result.order_id,
                stop_price=stop_price,
                target_order_id=target_order_id,
                target_price=target_price,
                # ITEM D (2026-07-08 hygiene batch): tag ladder-capability at
                # LIVE entry recording time, from the ORIGINAL filled quantity
                # -- the T1 60/40 profile split needs >= 2 contracts (see
                # _partial_quantity in profile_exit.py). This is a snapshot:
                # trade_sessions.quantity is later overwritten to the residual
                # by a partial bank, so it must be captured here, not derived
                # from quantity at report time. Metadata only -- never read by
                # order-management logic; the daily report is the consumer.
                can_ladder=plan.quantity >= 2,
            )
        )
        if protection_error is not None:
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "protective_stop_failure",
                    "symbol": deployment.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": plan.trade_id,
                    "option_symbol": plan.option_symbol,
                    "entry_order_id": plan.order_id,
                    "error": protection_error,
                    "stage": "initial_protection",
                },
            )
        if target_order_id and stop_result.order_id:
            transition = self.lifecycle_store.mark_target_active(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=target_order_id,
            )
            await self._emit_lifecycle_transition(transition, reason="entry_filled_target_active")
        else:
            transition = self.lifecycle_store.mark_open(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=stop_result.order_id or plan.order_id,
                protected=bool(stop_result.order_id),
            )
            await self._emit_lifecycle_transition(
                transition,
                reason="entry_filled_open_protected" if stop_result.order_id else "entry_filled_open_unprotected",
            )
        if protection_error is not None:
            risk_details["protection_error"] = protection_error
        return replace(plan, stop_order_id=stop_result.order_id, target_order_id=target_order_id, risk_details=risk_details)

    async def _wait_for_entry_fill_or_cancel(
        self,
        plan: TradePlan,
        deployment: DeploymentManifest,
    ) -> "_EntryWaitResult":
        if not _entry_reprice_enabled(self.app_config, deployment):
            return await self._wait_for_entry_fill_once(
                plan,
                timeout_seconds=self.app_config.order_fill_timeout_seconds,
                reprice_attempt=0,
            )

        started_at = datetime.now(UTC)
        active_plan = plan
        cancel_after_seconds = _entry_reprice_cancel_after_seconds(self.app_config, deployment)
        checkpoints = _entry_reprice_checkpoints(self.app_config, deployment)
        for attempt, checkpoint_seconds in enumerate(checkpoints, start=1):
            wait_seconds = _remaining_seconds(started_at, checkpoint_seconds)
            result = await self._wait_for_entry_fill_once(
                active_plan,
                timeout_seconds=wait_seconds,
                reprice_attempt=attempt - 1,
            )
            if result.filled or _terminal_entry_error(result.error):
                return result
            reprice = await self._reprice_live_entry(active_plan, deployment, attempt=attempt)
            if reprice.filled:
                return _EntryWaitResult(
                    plan=reprice.plan,
                    filled=True,
                    payload=reprice.payload,
                    error=None,
                )
            if reprice.error is not None:
                return _EntryWaitResult(
                    plan=reprice.plan,
                    filled=False,
                    payload=reprice.payload or result.payload,
                    error=reprice.error,
                    cancelled_without_fill=reprice.cancelled_without_fill,
                )
            active_plan = reprice.plan

        result = await self._wait_for_entry_fill_once(
            active_plan,
            timeout_seconds=_remaining_seconds(started_at, cancel_after_seconds),
            reprice_attempt=len(checkpoints),
        )
        if result.filled or _terminal_entry_error(result.error):
            return result

        cancel_result = await self._cancel_entry_order_and_check_fill(active_plan)
        if cancel_result.filled:
            await self.event_repository.append(
                "entry_reprice_cancel_race_filled",
                {
                    "deployment_id": active_plan.deployment_id,
                    "trade_id": active_plan.trade_id,
                    "order_id": active_plan.order_id,
                    "cancel_after_seconds": cancel_after_seconds,
                    "cancel_ok": cancel_result.cancel_ok,
                    "cancel_error": cancel_result.cancel_error,
                    "payload": cancel_result.payload or {},
                },
            )
            return _EntryWaitResult(
                plan=active_plan,
                filled=True,
                payload=cancel_result.payload,
                error=None,
            )
        await self.event_repository.append(
            "entry_reprice_cancel_after_timeout",
            {
                "deployment_id": active_plan.deployment_id,
                "trade_id": active_plan.trade_id,
                "order_id": active_plan.order_id,
                "cancel_after_seconds": cancel_after_seconds,
                "cancel_ok": cancel_result.cancel_ok,
                "cancel_error": cancel_result.cancel_error,
                "status": cancel_result.status,
                "status_error": cancel_result.status_error,
                "payload": result.payload or {},
            },
        )
        return _EntryWaitResult(
            plan=active_plan,
            filled=False,
            payload=result.payload,
            error="entry_reprice_cancel_after_timeout" if cancel_result.cancel_ok else f"entry_reprice_final_cancel_failed:{cancel_result.cancel_error}",
            cancelled_without_fill=cancel_result.cancel_ok,
        )

    async def _wait_for_entry_fill_once(
        self,
        plan: TradePlan,
        *,
        timeout_seconds: int,
        reprice_attempt: int,
    ) -> "_EntryWaitResult":
        filled, payload, error = await self.planner.order_manager.wait_for_fill(
            plan.order_id,
            timeout_seconds=max(int(timeout_seconds), 0),
            poll_seconds=self.app_config.order_fill_poll_seconds,
        )
        await self.event_repository.append(
            "entry_fill_check",
            {
                "deployment_id": plan.deployment_id,
                "order_id": plan.order_id,
                "filled": filled,
                "error": error,
                "reprice_attempt": reprice_attempt,
                "average_fill_price": _filled_entry_price(payload, fallback=plan.estimated_entry_price) if filled else None,
                "payload": payload or {},
            },
        )
        return _EntryWaitResult(plan=plan, filled=filled, payload=payload, error=error)

    async def _reprice_live_entry(
        self,
        plan: TradePlan,
        deployment: DeploymentManifest,
        *,
        attempt: int,
    ) -> "_EntryRepriceResult":
        pricing_params = deployment.execution.model_dump()
        spread_fraction = _entry_reprice_spread_fraction(deployment, attempt)
        if spread_fraction is None:
            pricing_params["entry_pricing_urgent_spread_pct"] = _entry_reprice_spread_pct(self.app_config, attempt)
        else:
            pricing_params["entry_pricing_spread_fraction"] = scale_spread_fraction(
                spread_fraction,
                enabled=deployment.execution.entry_pricing_oi_percentile_scale,
                open_interest_percentile=_risk_open_interest_percentile(plan),
            )
        try:
            quote = await self.planner.order_manager.get_option_quote(plan.option_symbol)
            pricing = select_entry_limit(quote, pricing_params)
        except Exception as exc:
            return await self._cancel_entry_for_reprice_block(
                plan,
                attempt=attempt,
                reason=f"entry_reprice_quote_unavailable:{exc}",
            )
        if pricing.block_reasons or pricing.limit_price is None:
            return await self._cancel_entry_for_reprice_block(
                plan,
                attempt=attempt,
                reason="entry_reprice_quote_blocked:" + ",".join(pricing.block_reasons or ["missing_limit"]),
                pricing_evidence=pricing.evidence(),
            )

        try:
            preflight = await self.planner.order_manager.preflight_entry(plan.option_symbol, pricing.limit_price, plan.quantity)
        except Exception as exc:
            return await self._cancel_entry_for_reprice_block(
                plan,
                attempt=attempt,
                reason=f"entry_reprice_preflight_failed:{exc}",
                pricing_evidence=pricing.evidence(),
            )
        final_limit_price = float(preflight.payload["limitPrice"])
        max_trade_premium = deployment.risk.max_trade_premium_usd or 300.0
        repriced_premium = final_limit_price * plan.quantity * 100
        if repriced_premium > max_trade_premium:
            return await self._cancel_entry_for_reprice_block(
                plan,
                attempt=attempt,
                reason="entry_reprice_above_max_trade_premium",
                pricing_evidence={
                    **pricing.evidence(),
                    "preflight_limit_price": final_limit_price,
                    "repriced_premium": repriced_premium,
                    "max_trade_premium_usd": max_trade_premium,
                },
            )
        required_cash = max(
            preflight.buying_power_requirement or 0.0,
            preflight.estimated_cost or 0.0,
            final_limit_price * plan.quantity * 100,
        )
        cancel_result = await self._cancel_entry_order_and_check_fill(plan)
        if cancel_result.filled:
            filled_price = _filled_entry_price(cancel_result.payload, fallback=plan.estimated_entry_price)
            filled_plan = replace(
                plan,
                estimated_entry_price=filled_price,
                risk_details={
                    **dict(plan.risk_details),
                    "entry_reprice_cancel_race_filled": True,
                    "broker_average_fill_price": filled_price,
                },
            )
            await self.event_repository.append(
                "entry_reprice_cancel_race_filled",
                {
                    "deployment_id": plan.deployment_id,
                    "trade_id": plan.trade_id,
                    "order_id": plan.order_id,
                    "attempt": attempt,
                    "cancel_ok": cancel_result.cancel_ok,
                    "cancel_error": cancel_result.cancel_error,
                    "pricing_evidence": pricing.evidence(),
                    "payload": cancel_result.payload or {},
                },
            )
            return _EntryRepriceResult(plan=filled_plan, filled=True, payload=cancel_result.payload)
        if not cancel_result.cancel_ok:
            await self.event_repository.append(
                "entry_reprice_cancel_failed",
                {
                    "deployment_id": plan.deployment_id,
                    "trade_id": plan.trade_id,
                    "order_id": plan.order_id,
                    "attempt": attempt,
                    "error": cancel_result.cancel_error,
                    "status": cancel_result.status,
                    "status_error": cancel_result.status_error,
                    "pricing_evidence": pricing.evidence(),
                },
            )
            return _EntryRepriceResult(plan=plan, error=f"entry_reprice_cancel_failed:{cancel_result.cancel_error}")

        cash_guard_details: dict[str, object] = {}
        if getattr(self.planner, "cash_guard", None) is not None:
            await self.planner.cash_guard.release_entry(plan.trade_id)
            cash_result = await self.planner.cash_guard.reserve_entry(
                trade_id=plan.trade_id,
                required_cash=required_cash,
                timestamp=plan.entry_timestamp or datetime.now(UTC),
            )
            cash_guard_details = dict(cash_result.details)
            if cash_result.blocked:
                await self.event_repository.append(
                    "entry_reprice_cash_guard_blocked",
                    {
                        "deployment_id": plan.deployment_id,
                        "trade_id": plan.trade_id,
                        "attempt": attempt,
                        "required_cash": required_cash,
                        "reason": cash_result.reason,
                        **cash_guard_details,
                    },
                )
                return _EntryRepriceResult(
                    plan=plan,
                    error=cash_result.reason or "entry_reprice_cash_guard_blocked",
                    cancelled_without_fill=True,
                )

        replacement_order_id = str(uuid.uuid4())
        result = await self.planner.order_manager.place_entry_order(
            plan.option_symbol,
            final_limit_price,
            plan.quantity,
            order_id=replacement_order_id,
        )
        if result.order_id is None:
            if getattr(self.planner, "cash_guard", None) is not None:
                await self.planner.cash_guard.release_entry(plan.trade_id)
            return _EntryRepriceResult(
                plan=plan,
                error=result.error or "entry_reprice_order_submit_failed",
                cancelled_without_fill=True,
            )

        pricing_evidence = {
            **pricing.evidence(),
            "preflight_limit_price": final_limit_price,
            "preflight_increment": preflight.current_increment,
            "preflight_buying_power_requirement": preflight.buying_power_requirement,
            "preflight_estimated_cost": preflight.estimated_cost,
        }
        risk_details = {
            **dict(plan.risk_details),
            "entry_pricing": pricing_evidence,
            "entry_reprice_attempt": attempt,
            "previous_entry_order_id": plan.order_id,
            "required_cash": required_cash,
            "buying_power_requirement": preflight.buying_power_requirement,
            "estimated_cost": preflight.estimated_cost,
            **cash_guard_details,
        }
        repriced_plan = replace(
            plan,
            order_id=result.order_id,
            estimated_entry_price=final_limit_price,
            risk_details=risk_details,
        )
        self.planner.position_tracker.open_position(
            deployment.symbol,
            deployment.deployment_id,
            trade_id=repriced_plan.trade_id,
            option_symbol=repriced_plan.option_symbol,
            quantity=repriced_plan.quantity,
            entry_price=repriced_plan.estimated_entry_price,
            underlying_entry_price=repriced_plan.underlying_entry_price,
            entry_timestamp=repriced_plan.entry_timestamp,
            source="live_pending",
            order_id=repriced_plan.order_id,
        )
        await self._upsert_trade_record(
            TradeRecord(
                trade_id=repriced_plan.trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                option_symbol=repriced_plan.option_symbol,
                quantity=repriced_plan.quantity,
                entry_price=repriced_plan.estimated_entry_price,
                underlying_entry_price=repriced_plan.underlying_entry_price,
                entry_timestamp=repriced_plan.entry_timestamp,
                status="pending_entry",
                entry_order_id=repriced_plan.order_id,
            )
        )
        transition = self.lifecycle_store.begin_entry(
            deployment.symbol,
            deployment.deployment_id,
            option_symbol=repriced_plan.option_symbol,
            order_id=repriced_plan.order_id,
        )
        await self._emit_lifecycle_transition(transition, reason="entry_repriced")
        await self.event_repository.append(
            "entry_order_repriced",
            {
                "deployment_id": repriced_plan.deployment_id,
                "trade_id": repriced_plan.trade_id,
                "attempt": attempt,
                "previous_order_id": plan.order_id,
                "replacement_order_id": repriced_plan.order_id,
                "previous_limit_price": plan.estimated_entry_price,
                "replacement_limit_price": final_limit_price,
                "pricing_evidence": pricing_evidence,
            },
        )
        return _EntryRepriceResult(plan=repriced_plan)

    async def _cancel_entry_for_reprice_block(
        self,
        plan: TradePlan,
        *,
        attempt: int,
        reason: str,
        pricing_evidence: dict[str, Any] | None = None,
    ) -> "_EntryRepriceResult":
        cancel_result = await self._cancel_entry_order_and_check_fill(plan)
        if cancel_result.filled:
            filled_price = _filled_entry_price(cancel_result.payload, fallback=plan.estimated_entry_price)
            filled_plan = replace(
                plan,
                estimated_entry_price=filled_price,
                risk_details={
                    **dict(plan.risk_details),
                    "entry_reprice_cancel_race_filled": True,
                    "broker_average_fill_price": filled_price,
                },
            )
            await self.event_repository.append(
                "entry_reprice_cancel_race_filled",
                {
                    "deployment_id": plan.deployment_id,
                    "trade_id": plan.trade_id,
                    "order_id": plan.order_id,
                    "attempt": attempt,
                    "reason": reason,
                    "cancel_ok": cancel_result.cancel_ok,
                    "cancel_error": cancel_result.cancel_error,
                    "pricing_evidence": pricing_evidence or {},
                    "payload": cancel_result.payload or {},
                },
            )
            return _EntryRepriceResult(plan=filled_plan, filled=True, payload=cancel_result.payload)
        await self.event_repository.append(
            "entry_reprice_blocked",
            {
                "deployment_id": plan.deployment_id,
                "trade_id": plan.trade_id,
                "order_id": plan.order_id,
                "attempt": attempt,
                "reason": reason,
                "cancel_ok": cancel_result.cancel_ok,
                "cancel_error": cancel_result.cancel_error,
                "status": cancel_result.status,
                "status_error": cancel_result.status_error,
                "pricing_evidence": pricing_evidence or {},
            },
        )
        return _EntryRepriceResult(
            plan=plan,
            error=reason if cancel_result.cancel_ok else f"entry_reprice_block_cancel_failed:{cancel_result.cancel_error}",
            cancelled_without_fill=cancel_result.cancel_ok,
        )

    async def _cancel_entry_order_and_check_fill(self, plan: TradePlan) -> "_EntryCancelResult":
        cancel_ok, cancel_error = await self.planner.order_manager.cancel_order(plan.order_id)
        try:
            status, payload, status_error = await asyncio.wait_for(
                self.planner.order_manager.get_order_status(plan.order_id),
                timeout=ENTRY_CANCEL_STATUS_READBACK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            status, payload, status_error = None, None, "cancel_status_readback_timeout"
        return _EntryCancelResult(
            cancel_ok=cancel_ok,
            cancel_error=cancel_error,
            status=status,
            payload=payload,
            status_error=status_error,
        )

    async def manage_open_position(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition | None:
        if position.source == "shadow" or deployment.execution.shadow_only:
            dry_run = True
        async with self._symbol_locks[position.symbol]:
            return await self._manage_open_position_locked(deployment, position, dry_run=dry_run)

    async def _manage_open_position_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition | None:
        if position.option_symbol is None or position.quantity <= 0:
            return None
        if position.entry_price is None:
            return None
        if position.exit_mode is not None or position.exit_order_id is not None:
            return position

        updated = position
        quote = None

        if updated.stop_order_id is None and updated.target_order_id is None:
            updated = await self._restore_missing_protection(deployment, updated, dry_run=dry_run)

        async def ensure_quote():
            nonlocal quote
            if quote is None:
                quote = await self.planner.order_manager.get_option_quote(updated.option_symbol)
            return quote
        if dry_run and updated.source == "shadow":
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            await self.event_repository.append(
                "shadow_mark",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": updated.symbol,
                    "trade_id": updated.trade_id,
                    "option_symbol": updated.option_symbol,
                    "quantity": updated.quantity,
                    "entry_price": updated.entry_price,
                    "mark_price": reference_price,
                    "bid": current_quote.bid,
                    "ask": current_quote.ask,
                    "last": current_quote.last,
                    "spread_pct": current_quote.spread_pct,
                    "unrealized_pnl_usd": _premium_pnl(updated.entry_price, reference_price, updated.quantity),
                    "unrealized_stop_r": _realized_stop_r(
                        updated.entry_price,
                        reference_price,
                        deployment.exit.stop_loss_pct or deployment.risk.stop_loss_pct,
                    ),
                },
            )
            if (
                reference_price is not None
                and updated.stop_price is not None
                and reference_price <= updated.stop_price
            ):
                decision = ExitDecision(
                    deployment_id=deployment.deployment_id,
                    symbol=updated.symbol,
                    timestamp=datetime.now(UTC),
                    exit=True,
                    action="square_off",
                    reason=["shadow_option_stop_loss"],
                    features={
                        "option_mark_price": reference_price,
                        "option_stop_price": updated.stop_price,
                    },
                )
                await self._handle_exit_locked(deployment, updated, decision, dry_run=True)
                return None
        if (
            _profit_target_configured(deployment)
            and updated.target_order_id is None
            and updated.target_price is None
        ):
            target_price = _deployment_target_price(deployment, position.entry_price)
            if self._supports_concurrent_exit_orders():
                target_order_id = "DRY_RUN_TARGET"
                target_error = None
                if not dry_run:
                    result = await self.planner.order_manager.place_target_order(position.option_symbol, target_price, position.quantity)
                    target_order_id = result.order_id
                    target_error = result.error
                await self.event_repository.append(
                    "profit_target_submission",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "target_order_id": target_order_id,
                        "target_error": target_error,
                        "target_price": target_price,
                        "source": "position_manager",
                    },
                )
                updated = _replace_position(updated, target_order_id=target_order_id, target_price=target_price)
            else:
                await self.event_repository.append(
                    "profit_target_armed",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "target_order_id": None,
                        "target_price": target_price,
                        "mode": "virtual",
                        "reason": "single_resting_exit_order_broker",
                        "source": "position_manager",
                    },
                )
                updated = _replace_position(updated, target_order_id=None, target_price=target_price)

        target_approach_offset_pct = deployment.exit.target_approach_offset_pct
        if (
            not self._supports_concurrent_exit_orders()
            and updated.target_price is not None
            and target_approach_offset_pct is not None
            and updated.target_order_id is None
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            activation_price = updated.target_price * (1.0 - target_approach_offset_pct)
            if reference_price is not None and reference_price >= activation_price:
                await self.event_repository.append(
                    "target_approach_detected",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated.symbol,
                        "option_symbol": updated.option_symbol,
                        "reference_price": reference_price,
                        "activation_price": activation_price,
                        "target_price": updated.target_price,
                        "target_approach_offset_pct": target_approach_offset_pct,
                        "stop_order_id": updated.stop_order_id,
                        "reason": "single_resting_exit_order_broker",
                    },
                )
                cancel_ok = True
                cancel_error = None
                canceled_stop_order_id = updated.stop_order_id
                if updated.stop_order_id and not dry_run:
                    cancel_ok, cancel_error = await self.planner.order_manager.cancel_order(updated.stop_order_id)
                    await self.event_repository.append(
                        "protection_cancel_attempt",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "stop_order_id": updated.stop_order_id,
                            "canceled": cancel_ok,
                            "error": cancel_error,
                            "reason": "virtual_target_activation",
                        },
                    )
                can_submit_target = dry_run or cancel_ok or self._allows_exit_submission_before_cancel_confirmation()
                if can_submit_target:
                    target_order_id = "DRY_RUN_TARGET_ACTIVATED"
                    target_error = cancel_error
                    if not dry_run:
                        result = await self.planner.order_manager.place_target_order(
                            updated.option_symbol,
                            updated.target_price,
                            updated.quantity,
                        )
                        target_order_id = result.order_id
                        target_error = result.error or target_error
                    await self.event_repository.append(
                        "virtual_target_activation",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "reference_price": reference_price,
                            "activation_price": activation_price,
                            "target_price": updated.target_price,
                            "canceled_stop_order_id": canceled_stop_order_id,
                            "target_order_id": target_order_id,
                            "target_error": target_error,
                        },
                    )
                    if target_order_id is not None:
                        target_activated_at = datetime.now(UTC)
                        updated = _replace_position(
                            updated,
                            stop_order_id=None,
                            target_order_id=target_order_id,
                            target_activation_price=reference_price,
                            target_activation_high_price=reference_price,
                            target_activated_at=target_activated_at,
                        )
                        transition = self.lifecycle_store.mark_target_active(
                            updated.symbol,
                            updated.deployment_id,
                            option_symbol=updated.option_symbol,
                            order_id=target_order_id,
                        )
                        await self._emit_lifecycle_transition(transition, reason="virtual_target_activation")

        target_pullback_restore_progress_pct = deployment.exit.target_pullback_restore_progress_pct
        if (
            not self._supports_concurrent_exit_orders()
            and updated.target_order_id is not None
            and updated.target_price is not None
            and target_pullback_restore_progress_pct is not None
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            restore_threshold = None
            if reference_price is not None:
                target_high_price = _target_handoff_high_price(
                    updated,
                    reference_price,
                    target_approach_offset_pct=deployment.exit.target_approach_offset_pct,
                )
                restore_threshold = _target_handoff_restore_threshold_price(
                    updated.entry_price,
                    target_high_price,
                    target_pullback_restore_progress_pct,
                )
                if _material_exit_price_change(updated.target_activation_high_price, target_high_price):
                    updated = _replace_position(
                        updated,
                        target_activation_price=updated.target_activation_price
                        or _target_handoff_activation_floor(
                            updated,
                            target_approach_offset_pct=deployment.exit.target_approach_offset_pct,
                        )
                        or reference_price,
                        target_activation_high_price=target_high_price,
                        target_activated_at=updated.target_activated_at or datetime.now(UTC),
                    )
            if reference_price is not None and reference_price <= restore_threshold:
                cancel_ok = True
                cancel_error = None
                canceled_target_order_id = updated.target_order_id
                if updated.target_order_id and not dry_run:
                    cancel_ok, cancel_error = await self.planner.order_manager.cancel_order(updated.target_order_id)
                    await self.event_repository.append(
                        "target_cancel_attempt",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "target_order_id": updated.target_order_id,
                            "canceled": cancel_ok,
                            "error": cancel_error,
                            "reason": "virtual_target_pullback_restore",
                        },
                    )
                can_restore_stop = dry_run or cancel_ok or self._allows_exit_submission_before_cancel_confirmation()
                if can_restore_stop:
                    restored_stop_price = updated.stop_price or (updated.entry_price * (1.0 - deployment.exit.stop_loss_pct))
                    stop_order_id = "DRY_RUN_RESTORED_STOP"
                    stop_error = cancel_error
                    if not dry_run:
                        result = await self.planner.order_manager.place_stop_loss_order(
                            updated.option_symbol,
                            restored_stop_price,
                            updated.quantity,
                        )
                        stop_order_id = result.order_id
                        stop_error = result.error or stop_error
                    await self.event_repository.append(
                        "virtual_target_pullback_restore",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "reference_price": reference_price,
                            "restore_threshold": restore_threshold,
                            "target_pullback_restore_progress_pct": target_pullback_restore_progress_pct,
                            "target_activation_price": updated.target_activation_price,
                            "target_activation_high_price": updated.target_activation_high_price,
                            "target_activated_at": updated.target_activated_at.isoformat()
                            if updated.target_activated_at is not None
                            else None,
                            "canceled_target_order_id": canceled_target_order_id,
                            "restored_stop_order_id": stop_order_id,
                            "restored_stop_price": restored_stop_price,
                            "stop_error": stop_error,
                        },
                    )
                    if stop_order_id is not None:
                        updated = _replace_position(
                            updated,
                            stop_order_id=stop_order_id,
                            stop_price=restored_stop_price,
                            target_order_id=None,
                            target_activation_price=None,
                            target_activation_high_price=None,
                            target_activated_at=None,
                        )
                        transition = self.lifecycle_store.mark_open(
                            updated.symbol,
                            updated.deployment_id,
                            option_symbol=updated.option_symbol,
                            order_id=stop_order_id,
                            protected=True,
                        )
                        await self._emit_lifecycle_transition(transition, reason="virtual_target_pullback_restore")

        if (
            deployment.exit.stop_to_breakeven_after_r_multiple is not None
            and (updated.stop_price is None or updated.stop_price + 1e-9 < updated.entry_price)
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            trigger_price = _target_price(
                updated.entry_price,
                deployment.exit.stop_loss_pct,
                deployment.exit.stop_to_breakeven_after_r_multiple,
            )
            if reference_price is not None and reference_price >= trigger_price:
                canceled_stop_order_id = updated.stop_order_id
                cancel_error = None
                if updated.stop_order_id and not dry_run:
                    canceled, cancel_error = await self.planner.order_manager.cancel_order(updated.stop_order_id)
                    if not canceled:
                        await self.event_repository.append(
                            "protection_cancel_attempt",
                            {
                                "deployment_id": deployment.deployment_id,
                                "symbol": updated.symbol,
                                "option_symbol": updated.option_symbol,
                                "stop_order_id": updated.stop_order_id,
                                "canceled": canceled,
                                "error": cancel_error,
                            },
                        )
                        return updated
                new_stop_order_id = "DRY_RUN_BREAKEVEN_STOP"
                new_stop_error = cancel_error
                if not dry_run:
                    result = await self.planner.order_manager.place_stop_loss_order(
                        updated.option_symbol,
                        updated.entry_price,
                        updated.quantity,
                    )
                    new_stop_order_id = result.order_id
                    new_stop_error = result.error
                await self.event_repository.append(
                    "breakeven_stop_promotion",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated.symbol,
                        "option_symbol": updated.option_symbol,
                        "reference_price": reference_price,
                        "trigger_price": trigger_price,
                        "canceled_stop_order_id": canceled_stop_order_id,
                        "new_stop_order_id": new_stop_order_id,
                        "new_stop_error": new_stop_error,
                        "new_stop_price": updated.entry_price,
                    },
                )
                updated = _replace_position(
                    updated,
                    stop_order_id=new_stop_order_id,
                    stop_price=updated.entry_price,
                )
                transition = self.lifecycle_store.mark_open(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=new_stop_order_id,
                    protected=True,
                )
                await self._emit_lifecycle_transition(transition, reason="breakeven_stop_promotion")

        if updated != position:
            self.planner.position_tracker.open_position(
                updated.symbol,
                updated.deployment_id,
                trade_id=updated.trade_id,
                option_symbol=updated.option_symbol,
                quantity=updated.quantity,
                entry_price=updated.entry_price,
                underlying_entry_price=updated.underlying_entry_price,
                entry_timestamp=updated.entry_timestamp,
                source=updated.source,
                order_id=updated.order_id,
                stop_order_id=updated.stop_order_id,
                stop_price=updated.stop_price,
                target_order_id=updated.target_order_id,
                target_price=updated.target_price,
                exit_order_id=updated.exit_order_id,
                exit_limit_price=updated.exit_limit_price,
                exit_submitted_at=updated.exit_submitted_at,
                exit_mode=updated.exit_mode,
                exit_reprice_count=updated.exit_reprice_count,
            )
            if updated.trade_id is not None and updated.option_symbol is not None:
                await self._upsert_trade_record(
                    TradeRecord(
                        trade_id=updated.trade_id,
                        deployment_id=updated.deployment_id,
                        symbol=updated.symbol,
                        option_symbol=updated.option_symbol,
                        quantity=updated.quantity,
                        entry_price=updated.entry_price,
                        underlying_entry_price=updated.underlying_entry_price,
                        entry_timestamp=updated.entry_timestamp,
                        status=_tracked_trade_status(updated),
                        entry_order_id=updated.order_id,
                        stop_order_id=updated.stop_order_id,
                        stop_price=updated.stop_price,
                        target_order_id=updated.target_order_id,
                        target_price=updated.target_price,
                        exit_order_id=updated.exit_order_id,
                        exit_limit_price=updated.exit_limit_price,
                        exit_submitted_at=updated.exit_submitted_at,
                        exit_mode=updated.exit_mode,
                    )
                )

        # PART A: SHADOW-RECORD DUAL-RUN + ARMED DISPATCH ROUTE. After the EXISTING
        # exit-management path has fully run, evaluate the operator exit profile for
        # this tick against the live quote already fetched above and RECORD it.
        #
        # With the operator flag ``profile_exit_drives_live`` OFF (its default and
        # the only state shipped) the fail-closed dispatch gate stays SHUT, so this
        # is record-only: it never alters ``updated``, places no orders, and never
        # routes into the exit/order path. When (and only when) the gate is open
        # the recorder DISPATCHES the mapped decision through the SAME
        # ``_handle_exit_locked`` dispatcher native exits use (Phase 2), inheriting
        # its idempotency / dry_run / order-placement safety. We are ALREADY inside
        # ``self._symbol_locks[symbol]`` here (acquired in ``manage_open_position``),
        # so the route uses ``_handle_exit_locked`` directly — calling the
        # lock-acquiring ``handle_exit`` would deadlock on the non-reentrant lock.
        # The route may close/modify the position, so propagate its result back as
        # the managed position. Failures here are isolated so the recorder/route can
        # never disrupt the real management path.
        if self._deployment_carries_exit_profile(deployment):
            try:
                shadow_quote = await ensure_quote()
                routed = await self._record_profile_exit_shadow(
                    deployment, updated, shadow_quote, dry_run=dry_run
                )
                # ``routed`` is None when the profile route fully closed the
                # position this tick; otherwise it is the (possibly stop-adjusted)
                # post-route position. Either way it supersedes ``updated``.
                if routed is None:
                    return None
                updated = routed
            except ProfileExitDispatchError:
                # An ARMED dispatch failure is a real exit failure, already surfaced
                # as a protective_stop_failure runtime_issue. PROPAGATE it (do NOT
                # swallow as a benign shadow error, do NOT return the stale position
                # as managed) so it behaves like a native exit failure. Unreachable
                # with the operator flag OFF (the gate never opens).
                raise
            except Exception as exc:  # never let shadow RECORDING break management
                await self.event_repository.append(
                    "profile_exit_shadow_error",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated.symbol,
                        "option_symbol": updated.option_symbol,
                        "trade_id": updated.trade_id,
                        "error": str(exc),
                    },
                )

        return updated

    def _supports_concurrent_exit_orders(self) -> bool:
        return bool(getattr(self.planner.order_manager, "supports_concurrent_exit_orders", False))

    def _allows_exit_submission_before_cancel_confirmation(self) -> bool:
        return bool(getattr(self.planner.order_manager, "allows_exit_submission_before_cancel_confirmation", False))

    async def handle_exit(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> ExitPlan | None:
        async with self._symbol_locks[position.symbol]:
            # DOUBLE-EXIT / AUTHORITY INVARIANT (the #1 risk). ``handle_exit`` is the
            # NATIVE exit entry (the runtime ``exit`` task / position-monitor
            # decisions). When the profile-exit dispatch gate is OPEN for this
            # position the PROFILE route is its sole exit authority and acts on it
            # within the same tick's ``manage`` (which runs BEFORE this serially-
            # queued ``exit`` task per the per-symbol dispatcher) — so the native
            # path must YIELD to avoid a double close or fighting stops. The verdict
            # is computed STATELESSLY from the same fail-closed gate the profile route
            # uses; it depends only on the deployment and ``position.source`` (fixed
            # at entry), so it is identical even though this native task carries a
            # stale pre-route position snapshot. The armed profile route reaches the
            # dispatcher via ``_handle_exit_locked`` (below), NOT this method, so it is
            # never blocked by this guard. With the operator flag OFF (the only state
            # shipped) the gate is always shut, so this guard never fires and native
            # exit authority is unchanged.
            if self._profile_exit_is_authoritative(deployment, position):
                await self.event_repository.append(
                    "native_exit_yielded_to_profile",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "trade_id": position.trade_id,
                        "action": decision.action,
                        "reason": decision.reason,
                    },
                )
                return None
            return await self._handle_exit_locked(deployment, position, decision, dry_run=dry_run)

    async def _handle_exit_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> ExitPlan | None:
        # ATTRIBUTION (workplan #10): a profile-dispatched decision is stamped
        # with features["profile_rule"] by profile_decision_to_exit_decision
        # (the sole producer of that key — see profile_exit.py). A native/legacy
        # thesis exit never sets it, so this is None on every pre-existing call
        # path and behavior there is byte-for-byte unchanged. Reporting-only: the
        # value is persisted to trade_sessions.exit_rule and consumed solely by
        # daily_report; nothing in order-management reads it.
        exit_rule = decision.features.get("profile_rule")
        await self.event_repository.append(
            "exit_decision",
            {
                "deployment_id": decision.deployment_id,
                "symbol": decision.symbol,
                "timestamp": decision.timestamp.isoformat(),
                "exit": decision.exit,
                "action": decision.action,
                "reason": decision.reason,
                "features": decision.features,
                "option_symbol": position.option_symbol,
                "quantity": position.quantity,
            },
        )
        if self.event_bus is not None:
            await self.event_bus.publish(ExitEvaluatedEvent(decision=decision))
        if not decision.exit or decision.action == "hold" or position.option_symbol is None or position.quantity <= 0:
            # H2: a hold-class decision may still carry a stop move
            # (STOP_TO_BREAKEVEN from the profile evaluator surfaces
            # replacement_stop_price with action="hold"/exit=False). Consume it so
            # the protective stop actually ratchets — independent of the unrelated
            # stop_to_breakeven_after_r_multiple config dial.
            if (
                decision.replacement_stop_price is not None
                and position.option_symbol is not None
                and position.quantity > 0
                and position.exit_mode is None
                and position.exit_order_id is None
            ):
                await self._apply_replacement_stop(deployment, position, decision, dry_run=dry_run)
            return None
        if decision.action != "square_off":
            return ExitPlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=deployment.deployment_id,
                symbol=position.symbol,
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                action=decision.action,
                reasons=decision.reason,
                dry_run=dry_run,
                canceled_stop_order_id=None,
                canceled_target_order_id=None,
                error=f"unsupported_exit_action:{decision.action}",
            )

        if position.exit_mode is not None or position.exit_order_id is not None:
            await self.event_repository.append(
                "exit_pending_status",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "exit_order_id": position.exit_order_id,
                    "status": "already_pending",
                    "exit_mode": position.exit_mode.value if position.exit_mode is not None else None,
                },
            )
            return None

        # C1: a partial-scale decision closes only part of the position and keeps
        # the residual runner open (stop/state intact). Routed to a dedicated
        # handler so the full-flatten paths below can never run for a partial.
        if _decision_is_partial_scale(decision):
            return await self._handle_partial_scale_locked(deployment, position, decision, dry_run=dry_run)

        updated_position = position
        canceled_stop_order_id = None
        canceled_target_order_id = None
        cancel_error = None
        if decision.cancel_protection_orders:
            (
                updated_position,
                canceled_stop_order_id,
                canceled_target_order_id,
                cancel_error,
            ) = await self._cancel_exit_protection(
                deployment,
                updated_position,
                dry_run=dry_run,
                reason="strategy_exit",
            )

        if dry_run:
            fill_details = await self._paper_exit_fill_details(updated_position, order_id="DRY_RUN_EXIT")
            self.planner.position_tracker.close_position(
                updated_position.symbol,
                updated_position.deployment_id,
                option_symbol=updated_position.option_symbol,
            )
            self.clear_profile_exit_state(updated_position)
            if updated_position.trade_id is not None:
                await self.trade_state_repository.mark_closed(
                    updated_position.trade_id, exit_rule=exit_rule, **fill_details
                )
            transition = self.lifecycle_store.mark_closed(updated_position.symbol, updated_position.deployment_id)
            await self._emit_lifecycle_transition(transition, reason="exit_closed")
            if updated_position.source == "shadow":
                await self._emit_shadow_exit_assumed(deployment, updated_position, fill_details, reason=decision.reason)
            plan = ExitPlan(
                trade_id=updated_position.trade_id or updated_position.order_id or "UNKNOWN_TRADE",
                deployment_id=deployment.deployment_id,
                symbol=updated_position.symbol,
                option_symbol=updated_position.option_symbol,
                quantity=updated_position.quantity,
                action=decision.action,
                reasons=decision.reason,
                dry_run=True,
                order_id="DRY_RUN_EXIT",
                canceled_stop_order_id=canceled_stop_order_id,
                canceled_target_order_id=canceled_target_order_id,
                error=cancel_error,
            )
            await self.event_repository.append("exit_plan", asdict(plan))
            await self._record_manual_status(
                deployment,
                stage="exit_closed",
                writer_call=self.manual_status_writer.mark_closed(
                    deployment,
                    trade_id=plan.trade_id,
                    note="dry_run_exit_closed",
                )
                if self.manual_status_writer is not None
                else None,
            )
            return plan

        updated_position, plan = await self._submit_exit_request(
            deployment,
            updated_position,
            exit_mode=ExitMode.STRATEGY,
            reason="exit_submitted",
            event_type="exit_submission",
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            inherited_error=cancel_error,
            exit_rule=exit_rule,
        )
        await self.event_repository.append("exit_plan", asdict(plan))
        await self._record_manual_status(
            deployment,
            stage="exit_submitted",
            writer_call=self.manual_status_writer.mark_exit_submitted(
                deployment,
                plan=plan,
            )
            if self.manual_status_writer is not None
            else None,
        )
        return plan

    async def _handle_partial_scale_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> ExitPlan | None:
        """Close a fraction of the position and keep the residual runner open (C1).

        The protective stop/target covers the *whole* position, so a partial bank
        cancels protection, sells ``close_qty`` contracts, and re-arms the stop on
        the residual at its prior stop price (the runner stays protected; the
        breakeven ratchet — if any — arrives as a separate STOP_TO_BREAKEVEN
        decision on a later tick, handled by ``_apply_replacement_stop``).

        Hard invariant: ``close_qty < position.quantity`` always — a PARTIAL_SCALE
        can never flatten the position (enforced by ``_resolve_exit_quantity``).
        """
        # ATTRIBUTION (workplan #10): see the matching comment in
        # ``_handle_exit_locked`` — None on every native/legacy call path.
        exit_rule = decision.features.get("profile_rule")
        close_qty = _resolve_exit_quantity(decision, position)  # raises if it would flatten
        residual_qty = position.quantity - close_qty
        # Defensive: the resolver guarantees this, but never proceed if the math
        # would not leave a residual.
        if residual_qty <= 0:
            raise ValueError(
                f"partial scale would leave residual {residual_qty}; refusing to flatten on a partial"
            )

        updated_position = position
        # MEDIUM-1: capture the prior resting stop price BEFORE cancelling
        # protection. ``_cancel_exit_protection`` now clears ``stop_price`` when it
        # clears ``stop_order_id``, so the residual stop derivation must read the
        # prior price from here (not from the post-cancel position) to preserve the
        # "residual inherits the prior stop price" behavior (NEW-1/NEW-2).
        prior_stop_price = position.stop_price
        canceled_stop_order_id = None
        canceled_target_order_id = None
        cancel_error = None
        if decision.cancel_protection_orders:
            (
                updated_position,
                canceled_stop_order_id,
                canceled_target_order_id,
                cancel_error,
            ) = await self._cancel_exit_protection(
                deployment,
                updated_position,
                dry_run=dry_run,
                reason="strategy_partial_scale",
            )

        # Size the close to the banked quantity only.
        partial_to_close = _replace_position(updated_position, quantity=close_qty)
        order_id: str | None
        error = cancel_error
        if dry_run:
            fill_details = await self._paper_exit_fill_details(partial_to_close, order_id="DRY_RUN_PARTIAL_SCALE")
            order_id = "DRY_RUN_PARTIAL_SCALE"
            if updated_position.source == "shadow":
                await self._emit_shadow_exit_assumed(deployment, partial_to_close, fill_details, reason=decision.reason)
        else:
            result = await self.planner.order_manager.place_close_order(
                updated_position.option_symbol,
                close_qty,
                exit_mode=ExitMode.STRATEGY,
            )
            order_id = result.order_id
            error = result.error or error
            if order_id is None:
                await self.event_repository.append(
                    "exit_submission_failure",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated_position.symbol,
                        "option_symbol": updated_position.option_symbol,
                        "quantity": close_qty,
                        "exit_mode": ExitMode.STRATEGY.value,
                        "order_type": "partial_scale",
                        "error": error,
                    },
                )
                restored_position = updated_position
                restored_stop_order_id = None
                restore_error = None
                if updated_position.option_symbol is not None and prior_stop_price is not None:
                    stop_result = await self.planner.order_manager.place_stop_loss_order(
                        updated_position.option_symbol,
                        prior_stop_price,
                        updated_position.quantity,
                    )
                    restored_stop_order_id = stop_result.order_id
                    restore_error = stop_result.error
                    if restored_stop_order_id is not None:
                        restored_position = _replace_position(
                            updated_position,
                            stop_order_id=restored_stop_order_id,
                            stop_price=prior_stop_price,
                        )
                    else:
                        await self.event_repository.append(
                            "runtime_issue",
                            {
                                "category": "protective_stop_failure",
                                "symbol": updated_position.symbol,
                                "deployment_id": deployment.deployment_id,
                                "trade_id": updated_position.trade_id,
                                "option_symbol": updated_position.option_symbol,
                                "error": restore_error or "partial_scale_failed_restore_stop_rejected",
                                "stage": "partial_scale_submission_failure_restore",
                            },
                        )
                self.planner.position_tracker.open_position(
                    restored_position.symbol,
                    restored_position.deployment_id,
                    trade_id=restored_position.trade_id,
                    option_symbol=restored_position.option_symbol,
                    quantity=restored_position.quantity,
                    entry_price=restored_position.entry_price,
                    underlying_entry_price=restored_position.underlying_entry_price,
                    entry_timestamp=restored_position.entry_timestamp,
                    source=restored_position.source,
                    order_id=restored_position.order_id,
                    stop_order_id=restored_position.stop_order_id,
                    stop_price=restored_position.stop_price,
                    target_order_id=restored_position.target_order_id,
                    target_price=restored_position.target_price,
                )
                if restored_position.trade_id is not None and restored_position.option_symbol is not None:
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=restored_position.trade_id,
                            deployment_id=restored_position.deployment_id,
                            symbol=restored_position.symbol,
                            option_symbol=restored_position.option_symbol,
                            quantity=restored_position.quantity,
                            entry_price=restored_position.entry_price,
                            underlying_entry_price=restored_position.underlying_entry_price,
                            entry_timestamp=restored_position.entry_timestamp,
                            status=_tracked_trade_status(restored_position),
                            entry_order_id=restored_position.order_id,
                            stop_order_id=restored_position.stop_order_id,
                            stop_price=restored_position.stop_price,
                            target_order_id=restored_position.target_order_id,
                            target_price=restored_position.target_price,
                        )
                    )
                transition = self.lifecycle_store.mark_open(
                    restored_position.symbol,
                    restored_position.deployment_id,
                    option_symbol=restored_position.option_symbol,
                    order_id=restored_position.stop_order_id or restored_position.order_id,
                    protected=bool(restored_position.stop_order_id),
                )
                await self._emit_lifecycle_transition(
                    transition,
                    reason="partial_scale_failed_position_restored"
                    if restored_position.stop_order_id
                    else "partial_scale_failed_position_unprotected",
                )
                plan = ExitPlan(
                    trade_id=restored_position.trade_id or restored_position.order_id or "UNKNOWN_TRADE",
                    deployment_id=deployment.deployment_id,
                    symbol=updated_position.symbol,
                    option_symbol=updated_position.option_symbol,
                    quantity=close_qty,
                    action="square_off",
                    reasons=decision.reason,
                    dry_run=False,
                    order_id=None,
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    error=error or restore_error or "partial_scale_order_submit_failed",
                )
                await self.event_repository.append("exit_plan", asdict(plan))
                return plan

        # ITEM B (2026-07-08 hygiene batch): durably record the banked leg's own
        # economics BEFORE the residual overwrites trade_sessions.quantity below.
        # Previously the only trace of this leg was an order_id in the
        # append-only partial_scale_submission event, with no fill confirmation
        # ever read back -- weekly per-leg P&L reconstruction had nothing durable
        # to read. dry_run/shadow already computed a synthetic fill via
        # _paper_exit_fill_details above; live legs are recorded unconfirmed here
        # and backfilled by sync_lifecycle's _enrich_pending_partial_fills sweep
        # once the broker confirms the fill (mirrors _enrich_recent_closed_exit_truth).
        if updated_position.trade_id is not None and updated_position.option_symbol is not None:
            await self.trade_state_repository.record_partial_fill(
                PartialFillRecord(
                    id=None,
                    trade_id=updated_position.trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=updated_position.symbol,
                    option_symbol=updated_position.option_symbol,
                    closed_quantity=close_qty,
                    order_id=order_id,
                    exit_rule=exit_rule,
                    submitted_at=datetime.now(UTC),
                    fill_price=fill_details.get("exit_price") if dry_run else None,
                    fill_quantity=fill_details.get("exit_filled_quantity") if dry_run else None,
                    filled_at=fill_details.get("exit_filled_at") if dry_run else None,
                    order_status=fill_details.get("exit_order_status") if dry_run else None,
                    order_type=fill_details.get("exit_order_type") if dry_run else None,
                    broker_payload=fill_details.get("exit_broker_payload") if dry_run else None,
                )
            )

        # The residual runner stays OPEN and MUST stay protected. A profile partial
        # cancels protection unconditionally (cancel_protection_orders=True), but
        # even when it does not we must never end with the residual naked OR with
        # two stops on it.
        residual = _replace_position(
            updated_position,
            quantity=residual_qty,
            stop_order_id=None,
            target_order_id=None,
            target_price=None,
            exit_order_id=None,
            exit_mode=None,
            exit_limit_price=None,
            exit_submitted_at=None,
        )

        # NEW-2 (double stop): if the decision did NOT cancel protection but the
        # position still carries a live stop, that stop covers the FULL original
        # quantity and would coexist with the residual stop we are about to place
        # -> two stops on one position. Cancel-then-replace: drop the stale
        # full-size stop first so exactly one stop ends up covering the residual.
        precanceled_residual_stop_id = None
        if not decision.cancel_protection_orders and updated_position.stop_order_id:
            precanceled_residual_stop_id = updated_position.stop_order_id
            if not dry_run:
                canceled, precancel_error = await self.planner.order_manager.cancel_order(
                    updated_position.stop_order_id
                )
                await self.event_repository.append(
                    "protection_cancel_attempt",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated_position.symbol,
                        "option_symbol": updated_position.option_symbol,
                        "stop_order_id": updated_position.stop_order_id,
                        "canceled": canceled,
                        "error": precancel_error,
                        "reason": "partial_scale_replace_full_size_stop",
                    },
                )
                if precancel_error and error is None:
                    error = precancel_error

        # NEW-1: ALWAYS re-arm a residual stop. Derive a price even when no prior
        # resting stop existed (from the profile stop in the decision diagnostics,
        # else the deployment exit spec). Never leave the residual stop_order_id
        # None after a successful partial.
        restored_stop_price = _residual_protective_stop_price(
            updated_position, decision, deployment, prior_stop_price=prior_stop_price
        )
        restored_stop_order_id = None
        residual_unprotected = False
        if restored_stop_price is None:
            # No entry/stop information anywhere to derive a price from. Record the
            # unprotected residual so the monitor's missing-protection path re-arms
            # on the next tick (it re-arms any open position with no stop/target).
            residual_unprotected = True
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "protective_stop_failure",
                    "symbol": residual.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": residual.trade_id,
                    "option_symbol": residual.option_symbol,
                    "error": "no_residual_stop_price_derivable",
                    "stage": "partial_scale_residual_protection",
                },
            )
        else:
            restored_stop_order_id = "DRY_RUN_PARTIAL_RESIDUAL_STOP"
            if not dry_run:
                stop_result = await self.planner.order_manager.place_stop_loss_order(
                    residual.option_symbol,
                    restored_stop_price,
                    residual_qty,
                )
                restored_stop_order_id = stop_result.order_id
                # NEW-3-style: a place-fail must not leave the residual naked with
                # only a logged error. Retry once; if still failing, mark the
                # residual for reprotection (stop_order_id/stop_price stay None so
                # the monitor's missing-protection path re-arms next tick) and
                # record the unprotected state.
                if restored_stop_order_id is None:
                    retry = await self.planner.order_manager.place_stop_loss_order(
                        residual.option_symbol,
                        restored_stop_price,
                        residual_qty,
                    )
                    restored_stop_order_id = retry.order_id
                    if restored_stop_order_id is None:
                        residual_unprotected = True
                        if error is None:
                            error = retry.error or stop_result.error or "residual_stop_place_failed"
                        await self.event_repository.append(
                            "runtime_issue",
                            {
                                "category": "protective_stop_failure",
                                "symbol": residual.symbol,
                                "deployment_id": deployment.deployment_id,
                                "trade_id": residual.trade_id,
                                "option_symbol": residual.option_symbol,
                                "error": retry.error or stop_result.error or "residual_stop_place_failed",
                                "stage": "partial_scale_residual_protection",
                            },
                        )
        if not residual_unprotected:
            residual = _replace_position(
                residual,
                stop_order_id=restored_stop_order_id,
                stop_price=restored_stop_price,
            )
        else:
            # MEDIUM-1 / NEW-6 parity: when the residual stop could not be placed
            # the residual must NOT keep a phantom ``stop_price``. ``residual`` was
            # built from ``updated_position`` with ``stop_order_id=None`` but it
            # still INHERITED the prior full-size stop's ``stop_price`` (the
            # ``_replace_position`` above did not touch it, and
            # ``_cancel_exit_protection`` clears ``stop_order_id`` without clearing
            # ``stop_price``). Force it to None so ``stop_order_id`` and
            # ``stop_price`` agree (both None) — otherwise a downstream
            # ``stop_price is not None`` protected-check is fooled into believing
            # the naked residual is protected and the monitor's missing-protection
            # path (stop_order_id is None AND target_order_id is None) skips it.
            residual = _replace_position(residual, stop_order_id=None, stop_price=None)

        # Persist the residual as the tracked position (same identity, reduced qty).
        self.planner.position_tracker.open_position(
            residual.symbol,
            residual.deployment_id,
            trade_id=residual.trade_id,
            option_symbol=residual.option_symbol,
            quantity=residual.quantity,
            entry_price=residual.entry_price,
            underlying_entry_price=residual.underlying_entry_price,
            entry_timestamp=residual.entry_timestamp,
            source=residual.source,
            order_id=residual.order_id,
            stop_order_id=residual.stop_order_id,
            stop_price=residual.stop_price,
            target_order_id=residual.target_order_id,
            target_price=residual.target_price,
        )
        if residual.trade_id is not None and residual.option_symbol is not None:
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=residual.trade_id,
                    deployment_id=residual.deployment_id,
                    symbol=residual.symbol,
                    option_symbol=residual.option_symbol,
                    quantity=residual.quantity,
                    entry_price=residual.entry_price,
                    underlying_entry_price=residual.underlying_entry_price,
                    entry_timestamp=residual.entry_timestamp,
                    status=_tracked_trade_status(residual),
                    entry_order_id=residual.order_id,
                    stop_order_id=residual.stop_order_id,
                    stop_price=residual.stop_price,
                    exit_rule=exit_rule,
                )
            )
        # Reflect the residual's protection state in the lifecycle store so the
        # monitor's missing-protection path re-arms an unprotected residual and the
        # desk surfaces it as open_unprotected (not silently open_protected).
        if residual.option_symbol is not None:
            transition = self.lifecycle_store.mark_open(
                residual.symbol,
                residual.deployment_id,
                option_symbol=residual.option_symbol,
                order_id=residual.stop_order_id or residual.order_id,
                protected=bool(residual.stop_order_id),
            )
            await self._emit_lifecycle_transition(
                transition,
                reason="partial_scale_residual_protected"
                if residual.stop_order_id
                else "partial_scale_residual_unprotected",
            )
        await self.event_repository.append(
            "partial_scale_submission",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": updated_position.symbol,
                "option_symbol": updated_position.option_symbol,
                "closed_quantity": close_qty,
                "residual_quantity": residual_qty,
                "order_id": order_id,
                "canceled_stop_order_id": canceled_stop_order_id,
                "canceled_target_order_id": canceled_target_order_id,
                "precanceled_residual_stop_order_id": precanceled_residual_stop_id,
                "restored_stop_order_id": restored_stop_order_id,
                "restored_stop_price": restored_stop_price if not residual_unprotected else None,
                "residual_protected": not residual_unprotected,
                "dry_run": dry_run,
                "reason": decision.reason,
                "error": error,
            },
        )
        plan = ExitPlan(
            trade_id=residual.trade_id or residual.order_id or "UNKNOWN_TRADE",
            deployment_id=deployment.deployment_id,
            symbol=updated_position.symbol,
            option_symbol=updated_position.option_symbol,
            quantity=close_qty,
            action="square_off",
            reasons=decision.reason,
            dry_run=dry_run,
            order_id=order_id,
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            error=error,
        )
        await self.event_repository.append("exit_plan", asdict(plan))
        return plan

    async def _apply_replacement_stop(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> TrackedPosition:
        """Move the protective stop to ``decision.replacement_stop_price`` (H2).

        Consumes the profile evaluator's STOP_TO_BREAKEVEN output: cancel the live
        stop and place a new one at the requested price via the existing stop
        primitive. Independent of the unrelated
        ``stop_to_breakeven_after_r_multiple`` config dial. No-op (returns the
        position unchanged) when there is nothing to move or the price is already
        in place.
        """
        new_stop_price = decision.replacement_stop_price
        if new_stop_price is None or position.option_symbol is None or position.quantity <= 0:
            return position
        if position.stop_price is not None and round(position.stop_price, 2) == round(new_stop_price, 2):
            return position  # already there; nothing to do

        canceled_stop_order_id = position.stop_order_id
        cancel_error = None
        if position.stop_order_id and not dry_run:
            canceled, cancel_error = await self.planner.order_manager.cancel_order(position.stop_order_id)
            if not canceled and not self._allows_exit_submission_before_cancel_confirmation():
                await self.event_repository.append(
                    "protection_cancel_attempt",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "stop_order_id": position.stop_order_id,
                        "canceled": canceled,
                        "error": cancel_error,
                        "reason": "profile_replacement_stop",
                    },
                )
                return position  # leave the old stop in place; do not go naked

        new_stop_order_id = "DRY_RUN_REPLACEMENT_STOP"
        new_stop_error = cancel_error
        if not dry_run:
            result = await self.planner.order_manager.place_stop_loss_order(
                position.option_symbol,
                new_stop_price,
                position.quantity,
            )
            new_stop_order_id = result.order_id
            new_stop_error = result.error or cancel_error
            # NEW-3: cancel-OK / place-fail naked window. We have already cancelled
            # (or ambiguously cancelled) the old stop, so a failed placement leaves
            # the position NAKED. Don't just log-and-ride: retry the placement once,
            # and if it still fails mark the position for reprotection so the next
            # monitor tick re-arms it (the monitor re-arms any open position with no
            # stop/target). The unprotected state is recorded below.
            if new_stop_order_id is None:
                retry = await self.planner.order_manager.place_stop_loss_order(
                    position.option_symbol,
                    new_stop_price,
                    position.quantity,
                )
                new_stop_order_id = retry.order_id
                new_stop_error = retry.error or new_stop_error

        # NEW-6: do NOT persist ``stop_price`` when the placement failed
        # (``stop_order_id is None``). Keeping a stale ``stop_price`` would fool
        # downstream ``stop_price is not None`` checks into believing the position
        # is protected. With both None, the monitor's missing-protection path
        # (stop_order_id is None and target_order_id is None) re-arms it next tick.
        persisted_stop_price = new_stop_price if new_stop_order_id is not None else None
        replacement_unprotected = new_stop_order_id is None and not dry_run
        updated = _replace_position(
            position, stop_order_id=new_stop_order_id, stop_price=persisted_stop_price
        )
        await self.event_repository.append(
            "profile_replacement_stop",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "option_symbol": position.option_symbol,
                "canceled_stop_order_id": canceled_stop_order_id,
                "new_stop_order_id": new_stop_order_id,
                "new_stop_price": persisted_stop_price,
                "new_stop_error": new_stop_error,
                "unprotected": replacement_unprotected,
                "reason": decision.reason,
                "dry_run": dry_run,
            },
        )
        if replacement_unprotected:
            # Keep recording the unprotected state so it is visible/auditable and
            # the desk surfaces it; the position is persisted below with
            # stop_order_id=None so the monitor re-protects on the next tick.
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "protective_stop_failure",
                    "symbol": position.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": position.trade_id,
                    "option_symbol": position.option_symbol,
                    "error": new_stop_error or "replacement_stop_place_failed",
                    "stage": "profile_replacement_stop",
                },
            )
        if updated.trade_id is not None and updated.option_symbol is not None:
            self.planner.position_tracker.open_position(
                updated.symbol,
                updated.deployment_id,
                trade_id=updated.trade_id,
                option_symbol=updated.option_symbol,
                quantity=updated.quantity,
                entry_price=updated.entry_price,
                underlying_entry_price=updated.underlying_entry_price,
                entry_timestamp=updated.entry_timestamp,
                source=updated.source,
                order_id=updated.order_id,
                stop_order_id=updated.stop_order_id,
                stop_price=updated.stop_price,
                target_order_id=updated.target_order_id,
                target_price=updated.target_price,
            )
            transition = self.lifecycle_store.mark_open(
                updated.symbol,
                updated.deployment_id,
                option_symbol=updated.option_symbol,
                order_id=new_stop_order_id or updated.order_id,
                protected=bool(new_stop_order_id),
            )
            await self._emit_lifecycle_transition(transition, reason="profile_replacement_stop")
        return updated

    async def _submit_exit_request(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        exit_mode: ExitMode,
        reason: str,
        event_type: str,
        canceled_stop_order_id: str | None = None,
        canceled_target_order_id: str | None = None,
        inherited_error: str | None = None,
        force_market: bool = False,
        submitted_at: datetime | None = None,
        increment_reprice: bool = False,
        exit_rule: str | None = None,
    ) -> tuple[TrackedPosition, ExitPlan]:
        if position.option_symbol is None:
            raise ValueError("Cannot submit exit without option_symbol")
        submitted_at = submitted_at or datetime.now(UTC)
        order_id: str | None = None
        limit_price: float | None = None
        error = inherited_error
        order_type = "MARKET" if exit_mode == ExitMode.EMERGENCY or force_market else "LIMIT"

        if exit_mode != ExitMode.EMERGENCY and not force_market:
            try:
                quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
            except Exception as exc:
                quote = None
                error = inherited_error or str(exc)
            limit_price = quote.exit_reference_price if quote is not None else None
            if limit_price is None:
                if exit_mode == ExitMode.STRATEGY:
                    order_type = "WAIT"
                else:
                    order_type = "MARKET"
        if order_type == "MARKET":
            if force_market and exit_mode != ExitMode.EMERGENCY:
                await self.event_repository.append(
                    "exit_market_fallback",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "exit_mode": exit_mode.value,
                        "reason": reason,
                    },
                )
            result = await self.planner.order_manager.place_close_order(
                position.option_symbol,
                position.quantity,
                exit_mode=exit_mode,
            )
            order_id = result.order_id
            error = result.error or error
            limit_price = None
        elif order_type == "LIMIT" and limit_price is not None:
            result = await self.planner.order_manager.place_close_order(
                position.option_symbol,
                position.quantity,
                exit_mode=exit_mode,
                limit_price=limit_price,
            )
            order_id = result.order_id
            error = result.error or error
        if order_type != "WAIT" and order_id is None:
            await self.event_repository.append(
                "exit_submission_failure",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "exit_mode": exit_mode.value,
                    "order_type": order_type,
                    "error": error,
                },
            )

        updated = _replace_position(
            position,
            exit_order_id=order_id,
            exit_limit_price=limit_price,
            exit_submitted_at=submitted_at,
            exit_mode=exit_mode,
            exit_reprice_count=position.exit_reprice_count + (1 if increment_reprice else 0),
        )
        self.planner.position_tracker.open_position(
            updated.symbol,
            updated.deployment_id,
            trade_id=updated.trade_id,
            option_symbol=updated.option_symbol,
            quantity=updated.quantity,
            entry_price=updated.entry_price,
            underlying_entry_price=updated.underlying_entry_price,
            entry_timestamp=updated.entry_timestamp,
            source=updated.source,
            order_id=updated.order_id,
            stop_order_id=updated.stop_order_id,
            stop_price=updated.stop_price,
            target_order_id=updated.target_order_id,
            target_price=updated.target_price,
            exit_order_id=updated.exit_order_id,
            exit_limit_price=updated.exit_limit_price,
            exit_submitted_at=updated.exit_submitted_at,
            exit_mode=updated.exit_mode,
            exit_reprice_count=updated.exit_reprice_count,
        )
        transition = self.lifecycle_store.mark_exit_pending(
            updated.symbol,
            updated.deployment_id,
            option_symbol=updated.option_symbol,
            order_id=updated.exit_order_id or updated.order_id,
        )
        await self._emit_lifecycle_transition(transition, reason=reason)
        if updated.trade_id is not None and updated.option_symbol is not None:
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=updated.trade_id,
                    deployment_id=updated.deployment_id,
                    symbol=updated.symbol,
                    option_symbol=updated.option_symbol,
                    quantity=updated.quantity,
                    entry_price=updated.entry_price,
                    underlying_entry_price=updated.underlying_entry_price,
                    entry_timestamp=updated.entry_timestamp,
                    status="exit_pending",
                    entry_order_id=updated.order_id,
                    stop_order_id=updated.stop_order_id,
                    stop_price=updated.stop_price,
                    target_order_id=updated.target_order_id,
                    target_price=updated.target_price,
                    exit_order_id=updated.exit_order_id,
                    exit_limit_price=updated.exit_limit_price,
                    exit_submitted_at=updated.exit_submitted_at,
                    exit_mode=updated.exit_mode,
                    exit_rule=exit_rule,
                )
            )
        await self.event_repository.append(
            event_type,
            {
                "deployment_id": deployment.deployment_id,
                "symbol": updated.symbol,
                "option_symbol": updated.option_symbol,
                "quantity": updated.quantity,
                "exit_mode": exit_mode.value,
                "order_id": updated.exit_order_id,
                "order_type": order_type,
                "limit_price": updated.exit_limit_price,
                "error": error,
                "exit_submitted_at": updated.exit_submitted_at.isoformat() if updated.exit_submitted_at is not None else None,
            },
        )
        plan = ExitPlan(
            trade_id=updated.trade_id or updated.order_id or "UNKNOWN_TRADE",
            deployment_id=updated.deployment_id,
            symbol=updated.symbol,
            option_symbol=updated.option_symbol,
            quantity=updated.quantity,
            action="square_off",
            reasons=[reason],
            dry_run=False,
            order_id=updated.exit_order_id,
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            error=error,
        )
        return updated, plan

    async def _cancel_exit_protection(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
        reason: str,
    ) -> tuple[TrackedPosition, str | None, str | None, str | None]:
        updated = position
        canceled_stop_order_id = None
        canceled_target_order_id = None
        first_error = None
        if position.stop_order_id:
            canceled = True
            cancel_error = None
            if not dry_run:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.stop_order_id)
            if canceled:
                canceled_stop_order_id = position.stop_order_id
                # MEDIUM-1: clear ``stop_price`` alongside ``stop_order_id`` so a
                # cancelled stop never leaves a phantom price behind. With the
                # order gone there is no live stop, and a stale ``stop_price``
                # would fool downstream ``stop_price is not None`` protected-checks.
                # (Callers that re-arm a new stop overwrite ``stop_price`` after.)
                updated = _replace_position(updated, stop_order_id=None, stop_price=None)
            elif self._allows_exit_submission_before_cancel_confirmation():
                await self.event_repository.append(
                    "ambiguous_cancel",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "order_id": position.stop_order_id,
                        "kind": "stop",
                        "reason": reason,
                        "error": cancel_error,
                    },
                )
            await self.event_repository.append(
                "protection_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "stop_order_id": position.stop_order_id,
                    "canceled": canceled,
                    "error": cancel_error,
                    "reason": reason,
                },
            )
            first_error = cancel_error
        if position.target_order_id:
            canceled = True
            cancel_error = None
            if not dry_run:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.target_order_id)
            if canceled:
                canceled_target_order_id = position.target_order_id
                updated = _replace_position(updated, target_order_id=None)
            elif self._allows_exit_submission_before_cancel_confirmation():
                await self.event_repository.append(
                    "ambiguous_cancel",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "order_id": position.target_order_id,
                        "kind": "target",
                        "reason": reason,
                        "error": cancel_error,
                    },
                )
            await self.event_repository.append(
                "target_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "target_order_id": position.target_order_id,
                    "canceled": canceled,
                    "error": cancel_error,
                    "reason": reason,
                },
            )
            if first_error is None:
                first_error = cancel_error
        return updated, canceled_stop_order_id, canceled_target_order_id, first_error

    async def _finalize_pending_exit_fill(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        exit_order_id: str | None,
        status: str | None,
        payload: dict | None,
        now: datetime,
        reason: str = "exit_filled",
    ) -> ExitPlan:
        """Close out a position whose exit order is confirmed FILLED and persist
        the fill truth (exit_price/exit_filled_quantity/exit_filled_at).

        ROOT CAUSE FIX (2026-07-08 hygiene batch, item A): this is the single
        chokepoint for recording a genuine exit fill, shared by the routine
        pending-exit FILLED poll AND the reprice cancel-race guard below
        (``_cancel_exit_order_and_check_fill`` callers). Previously the
        cancel-then-resubmit reprice paths did NOT check whether the order they
        were about to supersede had already filled; when a cancel raced a real
        fill (broker reports the cancel as failed/ambiguous *because* the order
        already filled), the code blindly resubmitted a new close order,
        overwriting ``exit_order_id`` with the new (non-filling) order and
        permanently losing the reference to the order that actually filled the
        position. The later broker-vanished reconciliation fallback
        (``_mark_disappeared_trade_closed`` / ``_find_terminal_exit_order_payload``)
        then had nothing left to find, so ``exit_price``/``exit_filled_quantity``/
        ``exit_filled_at`` never wrote back even though ``status`` correctly
        went to ``closed`` and ``exit_rule`` (persisted earlier, at submission
        time) survived. Routing every confirmed fill through this one method
        closes that gap regardless of which caller discovered it.
        """
        self.planner.position_tracker.close_position(
            position.symbol,
            position.deployment_id,
            option_symbol=position.option_symbol,
        )
        self.clear_profile_exit_state(position)
        if position.trade_id is not None:
            await self._mark_trade_closed_with_exit_truth(
                position.trade_id,
                exit_order_id=exit_order_id,
                status=status,
                payload=payload,
            )
        transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
        await self._emit_lifecycle_transition(transition, reason="exit_closed")
        plan = ExitPlan(
            trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
            deployment_id=deployment.deployment_id,
            symbol=position.symbol,
            option_symbol=position.option_symbol,
            quantity=position.quantity,
            action="square_off",
            reasons=[reason],
            dry_run=False,
            order_id=exit_order_id,
        )
        await self.event_repository.append("exit_plan", asdict(plan))
        await self._record_manual_status(
            deployment,
            stage="exit_closed",
            writer_call=self.manual_status_writer.mark_closed(
                deployment,
                trade_id=position.trade_id,
                note=reason,
                event_at=now,
            )
            if self.manual_status_writer is not None
            else None,
        )
        return plan

    async def _cancel_exit_order_and_check_fill(
        self, order_id: str
    ) -> tuple[bool, str | None, str | None, dict | None, str | None]:
        """Cancel a resting exit order and read back its status before treating
        it as safely superseded (E1: exit-side cancel-race fill guard, mirrors
        the entry-side ``_cancel_entry_order_and_check_fill``).

        A broker cancel can report failure/ambiguity precisely *because* the
        order already filled. Reading the status back after every cancel
        attempt (not just on a failed ``canceled`` return) lets a reprice
        caller detect that race and record the real fill instead of
        resubmitting a duplicate close order that would orphan the true
        ``exit_order_id``. Returns
        ``(canceled_ok, cancel_error, status, payload, status_error)``.
        ``status_error`` is non-None when the readback itself failed
        (``get_order_status`` swallows broker exceptions into an error string
        -- it never raises) or timed out; audit fix A.1 requires the caller to
        treat that as "order state unknown" and fail closed, and to log the
        error rather than discard it.
        """
        canceled_ok, cancel_error = await self.planner.order_manager.cancel_order(order_id)
        try:
            status, payload, status_error = await asyncio.wait_for(
                self.planner.order_manager.get_order_status(order_id),
                timeout=ENTRY_CANCEL_STATUS_READBACK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            status, payload, status_error = None, None, "cancel_status_readback_timeout"
        return canceled_ok, cancel_error, status, payload, status_error

    async def _resolve_exit_cancel_for_reprice(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        exit_order_id: str,
        reason: str,
        now: datetime,
    ) -> "_ExitCancelRaceOutcome":
        """Cancel a resting exit order ahead of a reprice and decide whether
        the reprice may proceed (audit fixes A.1 + A.2 on top of the item-A
        cancel-race guard). Shared by the STRATEGY and HARD_FLAT branches of
        ``_manage_pending_exit_locked``.

        Decision ladder, strictest first:

        1. FULL FILL (item A): readback shows ``FILLED``, or a dead order whose
           ``filledQuantity`` covers the order's own placed quantity (audit
           finding 1: NOT position.quantity, which reconciliation may have
           already shrunk past the fill) -- record the fill truth via
           ``_finalize_pending_exit_fill``; never resubmit.
        2. BLOCKED (A.1, fail closed): the cancel was NOT cleanly confirmed AND
           the readback did not cleanly prove the order dead (broker
           error/timeout/``None``/non-terminal status such as
           ``PARTIALLY_FILLED``). The order may still fill; resubmitting now is
           the pre-audit blind-resubmit bug. Emit ``exit_reprice_blocked``
           (including the previously-discarded ``status_error``) and skip this
           cycle -- the pending-exit poller retries within
           ``order_fill_poll_seconds`` (the fail-safe mirror of the entry
           side's ``_cancel_entry_for_reprice_block``).
        3. PARTIAL FILL (A.2, the consequential one): the order is dead (or the
           cancel cleanly acked) with ``0 < filledQuantity <`` the order's own
           placed quantity -- the common real-broker outcome of a cancel
           racing a working order. Durably record the filled leg in
           ``trade_partial_fills`` (origin="exit_cancel_race"; this path is
           distinct from the deliberate ``_handle_partial_scale_locked``
           banks) and return a position carrying only the RESIDUAL quantity
           (placed minus filled, audit finding 1) so the resubmit can never
           oversell.
        4. RESUBMIT: the order is confirmed dead unfilled, or the cancel was
           cleanly confirmed -- safe to place the replacement order at full
           quantity (pre-existing behavior).
        """
        canceled, cancel_error, status, payload, status_error = await self._cancel_exit_order_and_check_fill(
            exit_order_id
        )
        normalized_status = str(status or "").upper()
        filled_quantity = _maybe_int((payload or {}).get("filledQuantity"))
        # Audit finding 1 (2026-07-09, shared with the dead-status ladder):
        # compare filledQuantity against the dead order's OWN placed quantity,
        # never position.quantity. Reconciliation (_refresh_reconciliation,
        # ~15s cadence and always at startup) replaces tracker positions with
        # the broker's live quantity -- which already EXCLUDES a
        # partially-filled lot -- so subtracting the fill from
        # position.quantity double-counts it (spurious finalize at N=2/1
        # filled; residual undersell at N=3/1 filled). The payload carries the
        # order's placed quantity; position.quantity is only the fallback for
        # payloads that omit it (pre-fix behavior).
        order_quantity = _maybe_int((payload or {}).get("quantity")) or position.quantity

        # 1. Full fill.
        if normalized_status == "FILLED" or (filled_quantity is not None and filled_quantity >= order_quantity):
            await self.event_repository.append(
                "exit_reprice_cancel_race_filled",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "reason": reason,
                    "cancel_ok": canceled,
                    "cancel_error": cancel_error,
                },
            )
            plan = await self._finalize_pending_exit_fill(
                deployment,
                position,
                exit_order_id=exit_order_id,
                status=status,
                payload=payload,
                now=now,
                reason="exit_reprice_cancel_race_filled",
            )
            return _ExitCancelRaceOutcome(action="finalized", plan=plan)

        # 2. A.1 fail closed: cancel unconfirmed and order not provably dead.
        if not canceled and normalized_status not in _EXIT_ORDER_DEAD_STATUSES:
            await self.event_repository.append(
                "exit_reprice_blocked",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "reason": reason,
                    "cancel_error": cancel_error,
                    "status": status,
                    "status_error": status_error,
                },
            )
            return _ExitCancelRaceOutcome(action="blocked", cancel_error=cancel_error)

        # 3. A.2 partial fill on a dead-or-cancel-acked order. Residual is
        # derived from the order's own placed quantity (audit finding 1).
        if filled_quantity is not None and 0 < filled_quantity < order_quantity:
            residual_quantity = order_quantity - filled_quantity
            details = _exit_fill_details(payload, status=status)
            if position.trade_id is not None and position.option_symbol is not None:
                await self.trade_state_repository.record_partial_fill(
                    PartialFillRecord(
                        id=None,
                        trade_id=position.trade_id,
                        deployment_id=deployment.deployment_id,
                        symbol=position.symbol,
                        option_symbol=position.option_symbol,
                        closed_quantity=filled_quantity,
                        order_id=exit_order_id,
                        exit_rule=None,
                        submitted_at=position.exit_submitted_at,
                        fill_price=details["exit_price"],
                        fill_quantity=details["exit_filled_quantity"] or filled_quantity,
                        filled_at=details["exit_filled_at"],
                        order_status=details["exit_order_status"],
                        order_type=details["exit_order_type"],
                        broker_payload=details["exit_broker_payload"],
                        origin="exit_cancel_race",
                    )
                )
            await self.event_repository.append(
                "exit_cancel_race_partial_fill",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "reason": reason,
                    "filled_quantity": filled_quantity,
                    "residual_quantity": residual_quantity,
                    "fill_price": details["exit_price"],
                    "status": status,
                    "cancel_ok": canceled,
                    "cancel_error": cancel_error,
                },
            )
            return _ExitCancelRaceOutcome(
                action="resubmit",
                position=_replace_position(position, quantity=residual_quantity),
                cancel_error=cancel_error,
            )

        # 4. Confirmed dead unfilled, or cleanly canceled: full-quantity resubmit.
        return _ExitCancelRaceOutcome(action="resubmit", position=position, cancel_error=cancel_error)

    async def _resolve_dead_status_exit_for_resubmit(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        exit_order_id: str,
        status: str | None,
        payload: dict | None,
        now: datetime,
    ) -> "_ExitCancelRaceOutcome":
        """A resting exit order that a routine poll found DEAD
        (REJECTED/CANCELED/EXPIRED) is about to be resubmitted. Before doing
        so, consult the final broker payload's ``filledQuantity`` -- the same
        guard the reprice-cancel sites (``_resolve_exit_cancel_for_reprice``)
        already apply (audit A.2), extended here to the third resubmit site
        (item #21).

        Unlike the reprice sites this path issues NO cancel: the poll already
        proved the order terminal, so there is nothing to cancel. It blocks in
        exactly one case -- an unparseable ``filledQuantity`` (audit finding 3
        below). The ladder:

        0. BLOCKED (audit finding 3, fail closed): ``filledQuantity`` carries
           a NON-NULL value we cannot parse ("N/A", ""). The broker asserted
           a fill field we cannot read -- treating it as "unfilled" and
           resubmitting the full quantity is a potential oversell with no
           event trail. Emit a ``runtime_issue``
           (category="exit_fill_unparseable") and skip this cycle; the
           pending-exit poller re-reads the order within
           ``order_fill_poll_seconds``. JSON null does NOT block (audit
           round 2, 2026-07-09): null is Public's STANDARD zero-fill idiom on
           order objects -- empirically every real zero-fill order reads
           ``"filledQuantity": null`` (never key-absent, never "0") -- so
           blocking on it would wedge every ordinary dead-unfilled exit
           forever with an alert per poll. Null and a missing key both fall
           through to step 3 (full resubmit, pre-#21 semantics, matching how
           the merged reprice ladder treats a null fill everywhere else).
        1. FULL FILL: ``filledQuantity`` covers the order's OWN placed
           quantity (audit finding 1: NOT position.quantity -- reconciliation
           replaces tracker positions with the broker's live quantity, which
           already excludes the filled lot, so comparing against it
           double-counts the fill: spurious finalize / orphaned live
           contract). Record the fill truth via
           ``_finalize_pending_exit_fill`` and never resubmit.
        2. PARTIAL FILL (the consequential one, item #21): a dead order with
           ``0 < filledQuantity <`` its placed quantity -- e.g. one lot fills
           and the remainder is CANCELED/EXPIRED. Durably record the filled
           leg in ``trade_partial_fills`` (origin="exit_dead_status", distinct
           from the deliberate ``partial_scale`` banks and the reprice
           ``exit_cancel_race`` legs) and resubmit only the RESIDUAL (placed
           minus filled, audit finding 1) so the replacement can never
           oversell the position.
        3. RESUBMIT: parseable zero fill, null fill (Public's zero-fill
           idiom), or ``filledQuantity`` key absent -- confirmed dead
           unfilled, resubmit the full quantity (pre-existing behavior).
        """
        payload_map = payload or {}
        raw_filled_quantity = payload_map.get("filledQuantity")
        filled_quantity = _maybe_int(raw_filled_quantity)
        order_quantity = _maybe_int(payload_map.get("quantity")) or position.quantity

        # 0. Audit finding 3 (narrowed in round 2): fail closed only on a
        # present, NON-NULL, unparseable fill value. Null means zero fill.
        if raw_filled_quantity is not None and filled_quantity is None:
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "exit_fill_unparseable",
                    "symbol": position.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": position.trade_id,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "status": status,
                    "error": f"unparseable_filled_quantity:{raw_filled_quantity!r}",
                    "stage": "exit_dead_status_resubmit",
                },
            )
            return _ExitCancelRaceOutcome(action="blocked")

        # 1. Full fill hidden behind a dead status.
        if filled_quantity is not None and filled_quantity >= order_quantity:
            await self.event_repository.append(
                "exit_dead_status_filled",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "status": status,
                    "filled_quantity": filled_quantity,
                    "order_quantity": order_quantity,
                },
            )
            plan = await self._finalize_pending_exit_fill(
                deployment,
                position,
                exit_order_id=exit_order_id,
                status=status,
                payload=payload,
                now=now,
                reason="exit_dead_status_filled",
            )
            return _ExitCancelRaceOutcome(action="finalized", plan=plan)

        # 2. Partial fill on a dead order: record the leg, resubmit the
        # residual only. Residual = the order's own placed quantity minus the
        # fill (audit finding 1), never position.quantity minus the fill.
        if filled_quantity is not None and 0 < filled_quantity < order_quantity:
            residual_quantity = order_quantity - filled_quantity
            details = _exit_fill_details(payload, status=status)
            if position.trade_id is not None and position.option_symbol is not None:
                await self.trade_state_repository.record_partial_fill(
                    PartialFillRecord(
                        id=None,
                        trade_id=position.trade_id,
                        deployment_id=deployment.deployment_id,
                        symbol=position.symbol,
                        option_symbol=position.option_symbol,
                        closed_quantity=filled_quantity,
                        order_id=exit_order_id,
                        exit_rule=None,
                        submitted_at=position.exit_submitted_at,
                        fill_price=details["exit_price"],
                        fill_quantity=details["exit_filled_quantity"] or filled_quantity,
                        filled_at=details["exit_filled_at"],
                        order_status=details["exit_order_status"],
                        order_type=details["exit_order_type"],
                        broker_payload=details["exit_broker_payload"],
                        origin="exit_dead_status",
                    )
                )
            await self.event_repository.append(
                "exit_dead_status_partial_fill",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": exit_order_id,
                    "status": status,
                    "filled_quantity": filled_quantity,
                    "order_quantity": order_quantity,
                    "residual_quantity": residual_quantity,
                    "fill_price": details["exit_price"],
                },
            )
            return _ExitCancelRaceOutcome(
                action="resubmit",
                position=_replace_position(position, quantity=residual_quantity),
            )

        # 3. Confirmed dead unfilled: full-quantity resubmit (pre-existing behavior).
        return _ExitCancelRaceOutcome(action="resubmit", position=position)

    async def manage_pending_exits(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        now: datetime | None = None,
    ) -> list[ExitPlan]:
        plans: list[ExitPlan] = []
        current_now = now or datetime.now(UTC)
        for position in list(self.planner.position_tracker.active_positions()):
            if position.exit_mode is None and position.exit_order_id is None and position.exit_submitted_at is None:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None:
                continue
            async with self._symbol_locks[position.symbol]:
                plan = await self._manage_pending_exit_locked(deployment, position, now=current_now)
            if plan is not None:
                plans.append(plan)
        return plans

    async def _manage_pending_exit_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        now: datetime,
    ) -> ExitPlan | None:
        if position.option_symbol is None or position.quantity <= 0 or position.exit_mode is None:
            return None
        status = None
        payload = None
        error = None
        if position.exit_order_id:
            status, payload, error = await self.planner.order_manager.get_order_status(position.exit_order_id)
        await self.event_repository.append(
            "exit_pending_status",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "option_symbol": position.option_symbol,
                "exit_order_id": position.exit_order_id,
                "status": status,
                "error": error,
                "exit_mode": position.exit_mode.value,
            },
        )
        normalized = (status or "").upper()
        if normalized == "FILLED":
            return await self._finalize_pending_exit_fill(
                deployment,
                position,
                exit_order_id=position.exit_order_id,
                status=status,
                payload=payload,
                now=now,
                reason="exit_filled",
            )
        if error and position.exit_order_id is not None:
            await self.event_repository.append(
                "ambiguous_cancel",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": position.exit_order_id,
                    "kind": "exit_status",
                    "reason": "status_unavailable",
                    "error": error,
                },
            )
            return None
        if normalized in {"REJECTED", "CANCELED", "EXPIRED"} or position.exit_order_id is None:
            resubmit_position = position
            if position.exit_order_id is not None and normalized in _EXIT_ORDER_DEAD_STATUSES:
                # Item #21: the dead-status readback may hide a partial fill
                # (one lot filled, the remainder CANCELED/EXPIRED). Consult
                # filledQuantity via the same guard ladder the reprice-cancel
                # sites use before resubmitting -- otherwise the full stale
                # quantity is resubmitted and the position is oversold.
                outcome = await self._resolve_dead_status_exit_for_resubmit(
                    deployment,
                    position,
                    exit_order_id=position.exit_order_id,
                    status=status,
                    payload=payload,
                    now=now,
                )
                if outcome.action == "finalized":
                    return outcome.plan
                if outcome.action == "blocked":
                    # Audit finding 3: filledQuantity present but unparseable
                    # -- resubmitting could oversell. Skip this cycle; the
                    # position keeps its exit_order_id so the next poll
                    # re-reads the order.
                    return None
                resubmit_position = outcome.position or position
            _, plan = await self._submit_exit_request(
                deployment,
                resubmit_position,
                exit_mode=position.exit_mode,
                reason="exit_resubmitted",
                event_type="exit_resubmitted",
                inherited_error=error,
                submitted_at=position.exit_submitted_at,
                force_market=position.exit_mode == ExitMode.EMERGENCY,
            )
            return plan
        if normalized in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            if position.exit_mode == ExitMode.STRATEGY:
                try:
                    quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
                except Exception:
                    quote = None
                next_price = quote.exit_reference_price if quote is not None else None
                if next_price is not None and _material_exit_price_change(position.exit_limit_price, next_price):
                    replaced_position, _, _, cancel_error = await self._cancel_exit_protection(
                        deployment,
                        position,
                        dry_run=False,
                        reason="exit_reprice",
                    )
                    if position.exit_order_id:
                        # E1 + audit A.1/A.2: cancel with fill-race, partial-fill,
                        # and fail-closed handling -- see
                        # _resolve_exit_cancel_for_reprice for the decision ladder.
                        outcome = await self._resolve_exit_cancel_for_reprice(
                            deployment,
                            replaced_position,
                            exit_order_id=position.exit_order_id,
                            reason="exit_reprice",
                            now=now,
                        )
                        if outcome.action == "finalized":
                            return outcome.plan
                        if outcome.action == "blocked":
                            return None
                        replaced_position = outcome.position or replaced_position
                        if cancel_error is None:
                            cancel_error = outcome.cancel_error
                    _, plan = await self._submit_exit_request(
                        deployment,
                        replaced_position,
                        exit_mode=ExitMode.STRATEGY,
                        reason="exit_reprice",
                        event_type="exit_reprice",
                        inherited_error=cancel_error,
                        submitted_at=position.exit_submitted_at,
                    )
                    return plan
            if position.exit_mode == ExitMode.HARD_FLAT:
                if position.exit_reprice_count == 0:
                    resubmit_position = position
                    hard_flat_cancel_error = None
                    if position.exit_order_id:
                        # E1 + audit A.1/A.2: same guard as the STRATEGY branch --
                        # a hard-flat reprice must not orphan an already-filled
                        # exit_order_id, oversell a partially-filled one, or
                        # resubmit over an order in an unknown state.
                        outcome = await self._resolve_exit_cancel_for_reprice(
                            deployment,
                            position,
                            exit_order_id=position.exit_order_id,
                            reason="hard_flat_reprice",
                            now=now,
                        )
                        if outcome.action == "finalized":
                            return outcome.plan
                        if outcome.action == "blocked":
                            return None
                        resubmit_position = outcome.position or position
                        hard_flat_cancel_error = outcome.cancel_error
                    _, plan = await self._submit_exit_request(
                        deployment,
                        resubmit_position,
                        exit_mode=ExitMode.HARD_FLAT,
                        reason="hard_flat_reprice",
                        event_type="exit_reprice",
                        inherited_error=hard_flat_cancel_error,
                        submitted_at=position.exit_submitted_at,
                        increment_reprice=True,
                    )
                    return plan
                if _hard_flat_market_fallback_due(position.exit_submitted_at, now, deployment):
                    _, plan = await self._submit_exit_request(
                        deployment,
                        position,
                        exit_mode=ExitMode.HARD_FLAT,
                        reason="hard_flat_market_fallback",
                        event_type="exit_resubmitted",
                        submitted_at=position.exit_submitted_at,
                        force_market=True,
                    )
                    return plan
        return None

    async def close_due_positions(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        now: datetime,
        dry_run: bool,
        symbol: str | None = None,
    ) -> list[TradePlan]:
        closed: list[TradePlan] = []
        now_et = as_et_time(now)
        for position in self.planner.position_tracker.active_positions():
            if symbol is not None and position.symbol != symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None or position.option_symbol is None or position.quantity <= 0:
                continue
            if position.exit_mode is not None or position.exit_order_id is not None:
                continue
            position_dry_run = dry_run or position.source == "shadow"
            hard_flat_time = parse_time_text(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")
            if now_et < hard_flat_time:
                continue

            order_id = "DRY_RUN_CLOSE"
            error = None
            if position_dry_run:
                fill_details = await self._paper_exit_fill_details(position, order_id=order_id)
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                self.clear_profile_exit_state(position)  # NEW-4: EOD sweep is terminal
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, **fill_details)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="hard_flat_closed")
                if position.source == "shadow":
                    await self._emit_shadow_exit_assumed(deployment, position, fill_details, reason=["hard_flat_time_reached"])
                await self._record_manual_status(
                    deployment,
                    stage="hard_flat_closed",
                    writer_call=self.manual_status_writer.mark_closed(
                        deployment,
                        trade_id=position.trade_id,
                        note="hard_flat_closed",
                        event_at=now,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            else:
                updated_position, canceled_stop_order_id, canceled_target_order_id, cancel_error = await self._cancel_exit_protection(
                    deployment,
                    position,
                    dry_run=False,
                    reason="hard_flat",
                )
                updated_position, exit_plan = await self._submit_exit_request(
                    deployment,
                    updated_position,
                    exit_mode=ExitMode.HARD_FLAT,
                    reason="hard_flat_submitted",
                    event_type="exit_submission",
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    inherited_error=cancel_error,
                )
                order_id = exit_plan.order_id
                error = exit_plan.error
                await self._record_manual_status(
                    deployment,
                    stage="hard_flat_submitted",
                    writer_call=self.manual_status_writer.mark_exit_submitted(
                        deployment,
                        plan=exit_plan,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            trade_plan = TradePlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=position.deployment_id,
                symbol=position.symbol,
                direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                estimated_entry_price=0.0,
                risk_reasons=["hard_flat_time_reached"],
                dry_run=position_dry_run,
                order_id=order_id,
            )
            await self.event_repository.append(
                "hard_flat_submission",
                {
                    "deployment_id": position.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "order_id": order_id,
                    "error": error,
                },
            )
            closed.append(trade_plan)
        return closed

    async def halt_and_flatten_positions(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        dry_run: bool,
        symbol: str | None = None,
    ) -> list[TradePlan]:
        closed: list[TradePlan] = []
        for position in self.planner.position_tracker.active_positions():
            if symbol is not None and position.symbol != symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None or position.option_symbol is None or position.quantity <= 0:
                continue
            if position.exit_mode is not None or position.exit_order_id is not None:
                continue
            position_dry_run = dry_run or position.source == "shadow"

            order_id = "DRY_RUN_EMERGENCY_FLAT"
            error = None
            canceled_stop_order_id = None
            canceled_target_order_id = None
            if position.source == "live_pending" and position.order_id:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.order_id)
                await self.event_repository.append(
                    "entry_cancel_attempt",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "entry_order_id": position.order_id,
                        "canceled": canceled,
                        "error": cancel_error,
                        "reason": "halt_and_flatten",
                    },
                )
                if not canceled:
                    await self.event_repository.append(
                        "halt_and_flatten_failure",
                        {
                            "deployment_id": position.deployment_id,
                            "symbol": position.symbol,
                            "option_symbol": position.option_symbol,
                            "quantity": position.quantity,
                            "error": cancel_error,
                            "reason": "pending_entry_cancel_failed",
                        },
                    )
                    continue
                order_id = position.order_id
                error = cancel_error
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                self.clear_profile_exit_state(position)  # NEW-4: pending-entry cancel is terminal
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, exit_order_id=order_id)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="halt_and_flatten_pending_entry_canceled")
                trade_plan = TradePlan(
                    trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                    deployment_id=position.deployment_id,
                    symbol=position.symbol,
                    direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    estimated_entry_price=0.0,
                    risk_reasons=["halt_and_flatten_triggered"],
                    dry_run=dry_run,
                    order_id=order_id,
                )
                await self.event_repository.append(
                    "halt_and_flatten_submission",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "quantity": position.quantity,
                        "order_id": order_id,
                        "error": error,
                        "mode": "pending_entry_cancel",
                    },
                )
                closed.append(trade_plan)
                continue
            if position_dry_run:
                fill_details = await self._paper_exit_fill_details(position, order_id=order_id)
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                self.clear_profile_exit_state(position)  # NEW-4: halt-and-flatten is terminal
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, **fill_details)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="halt_and_flatten_closed")
                if position.source == "shadow":
                    await self._emit_shadow_exit_assumed(deployment, position, fill_details, reason=["halt_and_flatten_triggered"])
                await self._record_manual_status(
                    deployment,
                    stage="halt_and_flatten_closed",
                    writer_call=self.manual_status_writer.mark_closed(
                        deployment,
                        trade_id=position.trade_id,
                        note="halt_and_flatten_closed",
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            else:
                updated_position, canceled_stop_order_id, canceled_target_order_id, cancel_error = await self._cancel_exit_protection(
                    deployment,
                    position,
                    dry_run=False,
                    reason="halt_and_flatten",
                )
                updated_position, exit_plan = await self._submit_exit_request(
                    deployment,
                    updated_position,
                    exit_mode=ExitMode.EMERGENCY,
                    reason="halt_and_flatten_submitted",
                    event_type="exit_submission",
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    inherited_error=cancel_error,
                    force_market=True,
                )
                order_id = exit_plan.order_id
                error = exit_plan.error
                await self._record_manual_status(
                    deployment,
                    stage="halt_and_flatten_submitted",
                    writer_call=self.manual_status_writer.mark_exit_submitted(
                        deployment,
                        plan=exit_plan,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            trade_plan = TradePlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=position.deployment_id,
                symbol=position.symbol,
                direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                estimated_entry_price=0.0,
                risk_reasons=["halt_and_flatten_triggered"],
                dry_run=position_dry_run,
                order_id=order_id,
            )
            await self.event_repository.append(
                "halt_and_flatten_submission",
                {
                    "deployment_id": position.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "order_id": order_id,
                    "error": error,
                    "canceled_stop_order_id": canceled_stop_order_id,
                    "canceled_target_order_id": canceled_target_order_id,
                },
            )
            closed.append(trade_plan)
        return closed

    async def sync_lifecycle(self) -> None:
        transitions = self.lifecycle_store.sync_from_positions(self.planner.position_tracker.active_positions())
        for transition in transitions:
            await self._emit_lifecycle_transition(transition, reason="broker_reconciliation_sync")
        recent_trades = await self.trade_state_repository.get_recent_trades(limit=200)
        recent_trade_ids = {trade.trade_id for trade in recent_trades}
        open_trades = await self.trade_state_repository.get_open_trades()
        open_trades_by_id = {trade.trade_id: trade for trade in open_trades}
        active_trade_ids = {
            position.trade_id
            for position in self.planner.position_tracker.active_positions()
            if position.trade_id is not None
        }
        for trade in open_trades:
            if trade.status == "pending_entry":
                continue
            if trade.status == "pending_entry_reconcile":
                if trade.trade_id in active_trade_ids:
                    continue
                await self._reconcile_pending_entry_release(trade)
                continue
            if trade.trade_id not in active_trade_ids:
                if _is_paper_trade_record(trade):
                    self._recover_paper_trade(trade)
                    active_trade_ids.add(trade.trade_id)
                    await self.event_repository.append(
                        "paper_position_recovered",
                        {
                            "deployment_id": trade.deployment_id,
                            "symbol": trade.symbol,
                            "trade_id": trade.trade_id,
                            "option_symbol": trade.option_symbol,
                            "quantity": trade.quantity,
                            "entry_price": trade.entry_price,
                            "underlying_entry_price": trade.underlying_entry_price,
                            "entry_timestamp": trade.entry_timestamp.isoformat() if trade.entry_timestamp else None,
                            "source": _paper_trade_source(trade),
                            "reason": "sync_lifecycle_rehydrate",
                        },
                    )
                    continue
                await self._mark_disappeared_trade_closed(trade)
        await self._enrich_recent_closed_exit_truth(recent_trades)
        await self._enrich_pending_partial_fills()
        for position in self.planner.position_tracker.active_positions():
            if position.trade_id is None or position.option_symbol is None:
                continue
            if position.source == "broker_recovered" and position.trade_id not in recent_trade_ids:
                await self.event_repository.append(
                    "orphan_position_recovered",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "trade_id": position.trade_id,
                        "option_symbol": position.option_symbol,
                        "quantity": position.quantity,
                        "entry_price": position.entry_price,
                        "entry_timestamp": position.entry_timestamp.isoformat() if position.entry_timestamp else None,
                    },
                )
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=position.trade_id,
                    deployment_id=position.deployment_id,
                    symbol=position.symbol,
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    underlying_entry_price=position.underlying_entry_price,
                    entry_timestamp=position.entry_timestamp,
                    status=_tracked_trade_status(position),
                    entry_order_id=position.order_id,
                    stop_order_id=position.stop_order_id,
                    stop_price=position.stop_price,
                    target_order_id=position.target_order_id,
                    target_price=position.target_price,
                    exit_order_id=position.exit_order_id,
                    exit_limit_price=position.exit_limit_price,
                    exit_submitted_at=position.exit_submitted_at,
                    exit_mode=position.exit_mode,
                )
            )
            previous = open_trades_by_id.get(position.trade_id)
            if previous is not None and previous.status == "pending_entry_reconcile":
                await self.event_repository.append(
                    "entry_reconcile_recovered",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "trade_id": position.trade_id,
                        "option_symbol": position.option_symbol,
                        "entry_order_id": position.order_id,
                    },
                )
        await self._sync_cash_guard()

    async def _mark_disappeared_trade_closed(self, trade: TradeRecord) -> None:
        exit_order_id, status, payload = await self._find_terminal_exit_order_payload(trade)
        await self._mark_trade_closed_with_exit_truth(
            trade.trade_id,
            exit_order_id=exit_order_id or trade.exit_order_id,
            status=status,
            payload=payload,
        )
        # NEW-4: a disappeared (broker-vanished) position is terminal. Clear by full
        # identity so both the ``trade:{id}`` and the ``pos:`` fallback keys are
        # dropped — no profile-exit ladder state can survive the close.
        self._clear_profile_exit_state_for_identity(
            trade_id=trade.trade_id,
            order_id=trade.entry_order_id,
            deployment_id=trade.deployment_id,
            symbol=trade.symbol,
            option_symbol=trade.option_symbol,
        )
        if exit_order_id is not None and payload is not None:
            await self.event_repository.append(
                "exit_fill_enriched",
                {
                    "deployment_id": trade.deployment_id,
                    "symbol": trade.symbol,
                    "trade_id": trade.trade_id,
                    "option_symbol": trade.option_symbol,
                    "exit_order_id": exit_order_id,
                    "status": status,
                    "source": "disappeared_position_reconcile",
                    "payload": payload,
                },
            )

    async def _enrich_recent_closed_exit_truth(self, trades: list[TradeRecord]) -> None:
        for trade in trades:
            if trade.status != "closed" or trade.exit_price is not None:
                continue
            if not any((trade.exit_order_id, trade.stop_order_id, trade.target_order_id)):
                continue
            exit_order_id, status, payload = await self._find_terminal_exit_order_payload(trade)
            if exit_order_id is None or payload is None:
                continue
            await self._mark_trade_closed_with_exit_truth(
                trade.trade_id,
                exit_order_id=exit_order_id,
                status=status,
                payload=payload,
            )
            await self.event_repository.append(
                "exit_fill_enriched",
                {
                    "deployment_id": trade.deployment_id,
                    "symbol": trade.symbol,
                    "trade_id": trade.trade_id,
                    "option_symbol": trade.option_symbol,
                    "exit_order_id": exit_order_id,
                    "status": status,
                    "source": "recent_closed_retry",
                    "payload": payload,
                },
            )

    async def _enrich_pending_partial_fills(self) -> None:
        """Backfill confirmed fill truth for banked partial legs (ITEM B).

        ``_handle_partial_scale_locked`` fires the close order for a banked
        partial without waiting for it to fill (the async pending-exit-poll
        pattern used for full closes does not apply here -- a partial leg's
        ``exit_order_id`` is deliberately not carried on the residual
        position, see NEW-1/NEW-2 there). This sweep is the durable-side
        equivalent of ``_enrich_recent_closed_exit_truth``: poll every
        ``trade_partial_fills`` row still missing ``fill_price`` and record
        the broker's confirmed fill once available. Paper/dry-run legs are
        never polled -- their fill truth is written synchronously at
        submission time (see ``_is_paper_order_id``).

        Audit fix 3 hardening: this sweep runs inside ``sync_lifecycle`` under
        the reconciliation ``sync_lock``, so every per-row status poll carries
        a hard ``asyncio.wait_for`` timeout (the broker client's own timeout
        is ~30s -- a degraded-broker episode across a row backlog would
        otherwise stall reconciliation serially). Rows that will never resolve
        get a terminal give-up state instead of being re-polled forever: a
        dead-status readback with no fill abandons immediately
        (``terminal_status:<STATUS>``); every other unresolved poll
        (error/timeout/non-terminal or mismatched status) counts one attempt,
        and hitting ``_PARTIAL_FILL_ENRICH_MAX_ATTEMPTS`` abandons with
        ``max_poll_attempts:<last-error>``. Abandonment is durable
        (``abandoned_reason``), excluded from future sweeps, and surfaced via
        the ``partial_fill_enrich_abandoned`` event.
        """
        pending = await self.trade_state_repository.get_unconfirmed_partial_fills()
        for record in pending:
            if record.id is None or record.order_id is None or _is_paper_order_id(record.order_id):
                continue
            try:
                status, payload, error = await asyncio.wait_for(
                    self.planner.order_manager.get_order_status(record.order_id),
                    timeout=ENTRY_CANCEL_STATUS_READBACK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                status, payload, error = None, None, "partial_fill_status_readback_timeout"
            if error or payload is None:
                await self._register_partial_fill_poll_failure(record, error=error or "missing_status_payload")
                continue
            normalized = str(status or payload.get("status") or "").upper()
            details = _exit_fill_details(payload, status=status)
            has_fill = details["exit_price"] is not None and bool(details["exit_filled_quantity"])
            if _is_filled_exit_order(payload, status=status, option_symbol=record.option_symbol) or (
                normalized in _EXIT_ORDER_DEAD_STATUSES and has_fill
            ):
                # Full fill, or a dead order that still filled part of the leg
                # before dying -- record whatever truth the broker reports.
                await self.trade_state_repository.enrich_partial_fill(
                    record.id,
                    fill_price=details["exit_price"],
                    fill_quantity=details["exit_filled_quantity"],
                    filled_at=details["exit_filled_at"],
                    order_status=details["exit_order_status"],
                    order_type=details["exit_order_type"],
                    broker_payload=details["exit_broker_payload"],
                )
                await self.event_repository.append(
                    "partial_fill_enriched",
                    {
                        "deployment_id": record.deployment_id,
                        "symbol": record.symbol,
                        "trade_id": record.trade_id,
                        "option_symbol": record.option_symbol,
                        "order_id": record.order_id,
                        "closed_quantity": record.closed_quantity,
                        "status": status,
                        "payload": payload,
                    },
                )
                continue
            if normalized in _EXIT_ORDER_DEAD_STATUSES:
                # Order died without any fill: it can never be enriched.
                await self._abandon_partial_fill(record, reason=f"terminal_status:{normalized}")
                continue
            await self._register_partial_fill_poll_failure(
                record, error=f"non_terminal_status:{normalized or 'unknown'}"
            )

    async def _register_partial_fill_poll_failure(self, record: PartialFillRecord, *, error: str) -> None:
        """Count one unresolved enrichment poll; abandon at the ceiling (audit fix 3)."""
        if record.id is None:
            return
        if record.enrich_attempts + 1 >= _PARTIAL_FILL_ENRICH_MAX_ATTEMPTS:
            await self._abandon_partial_fill(record, reason=f"max_poll_attempts:{error}")
            return
        await self.trade_state_repository.increment_partial_fill_enrich_attempts(record.id)

    async def _abandon_partial_fill(self, record: PartialFillRecord, *, reason: str) -> None:
        """Durably stop re-polling a partial leg that will never resolve (audit fix 3).

        Item #22: an abandonment is permanent fill-detail loss that needs a
        human backfill, so besides the diagnostic ``partial_fill_enrich_abandoned``
        row it is ALSO escalated as a ``runtime_issue`` -- the one channel that
        daily_report aggregates into ``runtime_issue_counts`` and surfaces in
        the daily report and Telegram summary. Without this the loss stays
        buried in the events table where nobody sees it. Category mirrors the
        supervisor's own hardcoded-category convention (cf.
        ``protective_stop_failure``).

        Ordering (audit finding 4, 2026-07-09): both events are appended
        BEFORE the durable ``mark_partial_fill_abandoned``. The mark is what
        excludes the row from future sweeps -- if it landed first and the
        process crashed before the events, the escalation would be lost
        forever (the row is never re-visited). With the mark last, a crash
        in between means the next sweep redoes the abandonment and re-emits:
        a rare duplicate escalation beats a permanently lost one.
        """
        if record.id is None:
            return
        await self.event_repository.append(
            "runtime_issue",
            {
                "category": "partial_fill_abandoned",
                "symbol": record.symbol,
                "deployment_id": record.deployment_id,
                "trade_id": record.trade_id,
                "option_symbol": record.option_symbol,
                "order_id": record.order_id,
                "closed_quantity": record.closed_quantity,
                "error": reason,
                "stage": "partial_fill_enrichment_sweep",
            },
        )
        await self.event_repository.append(
            "partial_fill_enrich_abandoned",
            {
                "deployment_id": record.deployment_id,
                "symbol": record.symbol,
                "trade_id": record.trade_id,
                "option_symbol": record.option_symbol,
                "order_id": record.order_id,
                "closed_quantity": record.closed_quantity,
                "origin": record.origin,
                "enrich_attempts": record.enrich_attempts,
                "reason": reason,
            },
        )
        await self.trade_state_repository.mark_partial_fill_abandoned(record.id, reason=reason)

    async def _find_terminal_exit_order_payload(self, trade: TradeRecord) -> tuple[str | None, str | None, dict | None]:
        seen: set[str] = set()
        order_ids = [
            trade.exit_order_id,
            trade.stop_order_id,
            trade.target_order_id,
        ]
        for order_id in order_ids:
            if not order_id or order_id in seen:
                continue
            if _is_paper_order_id(order_id):
                continue
            seen.add(order_id)
            status, payload, error = await self.planner.order_manager.get_order_status(order_id)
            if error or payload is None:
                continue
            # Audit finding 2 (2026-07-09): a dead order (CANCELED/EXPIRED/
            # REJECTED) that filled before dying carries real exit truth too --
            # a finalize that ran before the broker published averagePrice
            # would otherwise leave exit_price NULL forever, because this
            # retry only accepted status==FILLED. Mirror the enrichment
            # sweep's dead+has_fill rule (with the same identity checks).
            if _is_filled_exit_order(payload, status=status, option_symbol=trade.option_symbol) or (
                _is_dead_exit_order_with_fill(payload, status=status, option_symbol=trade.option_symbol)
            ):
                return order_id, status, payload
        return None, None, None

    async def _mark_trade_closed_with_exit_truth(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None,
        status: str | None,
        payload: dict | None,
    ) -> None:
        details = _exit_fill_details(payload, status=status)
        await self.trade_state_repository.mark_closed(
            trade_id,
            exit_order_id=exit_order_id,
            **details,
        )
        # NEW-4: this is a terminal close (reconciled fill truth) and the common
        # chokepoint for the pending-exit FILLED path and the disappeared-position
        # reconcile. Drop the profile-exit ladder state so it cannot leak past a
        # terminal close. The state is keyed primarily by trade id (see
        # ``_profile_state_key``), so clearing ``trade:{trade_id}`` covers any
        # position whose identity is its trade id.
        if trade_id:
            self._profile_exit_states.pop(f"trade:{trade_id}", None)

    async def _reconcile_pending_entry_release(self, trade: TradeRecord) -> None:
        if trade.entry_order_id is None:
            return
        status, payload, error = await self.planner.order_manager.get_order_status(trade.entry_order_id)
        normalized = (status or error or "").upper()
        if normalized not in {"REJECTED", "CANCELED", "EXPIRED"}:
            return
        await self._release_cash_guard_reservation(trade.trade_id)
        await self.trade_state_repository.mark_closed(trade.trade_id, exit_order_id=trade.exit_order_id)
        # NEW-4: releasing a reconcile-hold entry is terminal (the entry never
        # filled / was rejected-cancelled-expired). Clear any profile-exit ladder
        # state by full identity so it cannot leak past this close.
        self._clear_profile_exit_state_for_identity(
            trade_id=trade.trade_id,
            order_id=trade.entry_order_id,
            deployment_id=trade.deployment_id,
            symbol=trade.symbol,
            option_symbol=trade.option_symbol,
        )
        transition = self.lifecycle_store.mark_closed(trade.symbol, trade.deployment_id)
        await self._emit_lifecycle_transition(transition, reason="entry_reconcile_released")
        await self.event_repository.append(
            "entry_reconcile_released",
            {
                "deployment_id": trade.deployment_id,
                "symbol": trade.symbol,
                "trade_id": trade.trade_id,
                "entry_order_id": trade.entry_order_id,
                "status": normalized,
                "payload": payload or {},
            },
        )

    def _recover_paper_trade(self, trade: TradeRecord) -> None:
        self.planner.position_tracker.open_position(
            trade.symbol,
            trade.deployment_id,
            trade_id=trade.trade_id,
            option_symbol=trade.option_symbol,
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            underlying_entry_price=trade.underlying_entry_price,
            entry_timestamp=trade.entry_timestamp,
            source=_paper_trade_source(trade),
            order_id=trade.entry_order_id,
            stop_order_id=trade.stop_order_id,
            stop_price=trade.stop_price,
            target_order_id=trade.target_order_id,
            target_price=trade.target_price,
            exit_order_id=trade.exit_order_id,
            exit_limit_price=trade.exit_limit_price,
            exit_submitted_at=trade.exit_submitted_at,
            exit_mode=trade.exit_mode,
        )

    async def _restore_missing_protection(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition:
        stop_loss_pct, policy = _resolved_recovery_stop_loss_pct(deployment)
        if stop_loss_pct is None or stop_loss_pct <= 0:
            return position
        existing_protection = None if dry_run else await self._find_active_close_order(position.option_symbol)
        if existing_protection is not None:
            updated = _replace_position(
                position,
                stop_order_id=existing_protection["order_id"] if existing_protection["type"] == "STOP" else None,
                stop_price=existing_protection["price"] if existing_protection["type"] == "STOP" else None,
                target_order_id=existing_protection["order_id"] if existing_protection["type"] == "LIMIT" else None,
                target_price=existing_protection["price"] if existing_protection["type"] == "LIMIT" else None,
            )
            await self.event_repository.append(
                "protection_restore_skipped",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "trade_id": position.trade_id,
                    "option_symbol": position.option_symbol,
                    "reason": "active_close_order_exists",
                    "order_id": existing_protection["order_id"],
                    "order_type": existing_protection["type"],
                    "price": existing_protection["price"],
                },
            )
            transition = (
                self.lifecycle_store.mark_target_active(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=updated.target_order_id,
                )
                if updated.target_order_id
                else self.lifecycle_store.mark_open(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=updated.stop_order_id,
                    protected=True,
                )
            )
            await self._emit_lifecycle_transition(transition, reason="protection_reconciled")
            return updated
        await self.event_repository.append(
            "protection_restore_attempt",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "policy": policy,
                "dry_run": dry_run,
            },
        )
        stop_result, stop_price, target_order_id, target_price = await self._arm_position_protection(
            deployment,
            option_symbol=position.option_symbol,
            quantity=position.quantity,
            entry_price=position.entry_price,
            dry_run=dry_run,
            event_payload={
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "policy": policy,
                "source": position.source,
                "dry_run": dry_run,
            },
        )
        if stop_result.order_id is None:
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "protection_restore",
                    "symbol": position.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": position.trade_id,
                    "error": stop_result.error or "missing_stop_order_id",
                    "stage": "protection_restore",
                },
            )
            return position
        updated = _replace_position(
            position,
            stop_order_id=stop_result.order_id,
            stop_price=stop_price,
            target_order_id=target_order_id,
            target_price=target_price,
        )
        transition = (
            self.lifecycle_store.mark_target_active(
                updated.symbol,
                updated.deployment_id,
                option_symbol=updated.option_symbol,
                order_id=target_order_id,
            )
            if target_order_id
            else self.lifecycle_store.mark_open(
                updated.symbol,
                updated.deployment_id,
                option_symbol=updated.option_symbol,
                order_id=stop_result.order_id,
                protected=True,
            )
        )
        await self._emit_lifecycle_transition(transition, reason="protection_restored")
        return updated

    async def _find_active_close_order(self, option_symbol: str | None) -> dict[str, object] | None:
        if option_symbol is None:
            return None
        try:
            portfolio = await self.planner.order_manager.get_portfolio()
        except Exception:
            return None
        normalized_symbol = normalize_option_symbol(option_symbol)
        for order in portfolio.get("orders", []) or []:
            instrument = order.get("instrument", {}) or {}
            if instrument.get("type") != "OPTION":
                continue
            if normalize_option_symbol(str(instrument.get("symbol", ""))) != normalized_symbol:
                continue
            if str(order.get("side", "")).upper() != "SELL":
                continue
            if str(order.get("openCloseIndicator", "")).upper() != "CLOSE":
                continue
            if str(order.get("status", "")).upper() not in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
                continue
            order_type = str(order.get("type", "")).upper()
            if order_type == "STOP":
                price = _maybe_float(order.get("stopPrice"))
            elif order_type == "LIMIT":
                price = _maybe_float(order.get("limitPrice"))
            else:
                continue
            order_id = order.get("orderId")
            if not order_id:
                continue
            return {"order_id": str(order_id), "type": order_type, "price": price}
        return None

    async def _arm_position_protection(
        self,
        deployment: DeploymentManifest,
        *,
        option_symbol: str,
        quantity: int,
        entry_price: float,
        dry_run: bool,
        event_payload: dict[str, object],
        emit_suppressed_event: bool = False,
    ):
        stop_loss_pct, _ = _resolved_recovery_stop_loss_pct(deployment)
        requested_stop_price = entry_price * (1.0 - (stop_loss_pct or 0.0))
        stop_price = requested_stop_price
        quote_bid = None
        stop_sanitized = False
        stop_sanitized_reason = None
        if event_payload.get("source") == "broker_sync":
            stop_price, quote_bid, stop_sanitized_reason = await self._sanitize_recovered_stop_price(
                option_symbol,
                requested_stop_price,
            )
            stop_sanitized = stop_sanitized_reason is not None
        if stop_price is None:
            stop_result = OrderResult(order_id=None, error="recovered_stop_sanitization_failed")
        else:
            stop_result = (
                _DryRunOrderResult("DRY_RUN_STOP")
                if dry_run
                else await self.planner.order_manager.place_stop_loss_order(option_symbol, stop_price, quantity)
            )
        target_order_id = None
        target_price = None
        if _profit_target_configured(deployment):
            target_price = _deployment_target_price(deployment, entry_price)
        elif emit_suppressed_event and _profit_target_would_be_configured_absent_profile_rule(deployment):
            # OPERATOR RULE (2026-07-02): the profile ladder owns profit-taking for
            # this deployment, so the full-size target that WOULD have been armed
            # (NVDA/AMD 2026-07-02 style) was suppressed. Entry-frequency signal
            # only (one per entry, not per tick) — see ``_profit_target_configured``.
            await self.event_repository.append(
                "profit_target_suppressed",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": deployment.symbol,
                    "reason": "profile_owns_profit_taking",
                },
            )
        if target_price is not None:
            if self._supports_concurrent_exit_orders():
                target_result = (
                    _DryRunOrderResult("DRY_RUN_TARGET")
                    if dry_run
                    else await self.planner.order_manager.place_target_order(option_symbol, target_price, quantity)
                )
                target_order_id = target_result.order_id
                await self.event_repository.append(
                    "profit_target_submission",
                    {
                        **event_payload,
                        "target_order_id": target_result.order_id,
                        "target_error": target_result.error,
                        "target_price": target_price,
                    },
                )
            else:
                await self.event_repository.append(
                    "profit_target_armed",
                    {
                        **event_payload,
                        "target_order_id": None,
                        "target_price": target_price,
                        "mode": "virtual",
                        "reason": "single_resting_exit_order_broker",
                    },
                )
        await self.event_repository.append(
            "protective_stop_submission",
            {
                **event_payload,
                "stop_order_id": stop_result.order_id,
                "stop_error": stop_result.error,
                "stop_price": stop_price,
                "requested_stop_price": round_price(requested_stop_price),
                "quote_bid": quote_bid,
                "stop_sanitized": stop_sanitized,
                "stop_sanitized_reason": stop_sanitized_reason,
            },
        )
        return stop_result, stop_price, target_order_id, target_price

    async def _sanitize_recovered_stop_price(
        self,
        option_symbol: str,
        requested_stop_price: float,
    ) -> tuple[float | None, float | None, str | None]:
        quote = await self.planner.order_manager.get_option_quote(option_symbol)
        if quote.bid is None:
            return round_price(requested_stop_price), None, None
        requested = round_price(requested_stop_price)
        max_valid_stop = _max_valid_sell_stop_price(quote.bid)
        if max_valid_stop is None:
            return None, quote.bid, "no_valid_bid_buffer"
        if requested < quote.bid:
            return requested, quote.bid, None
        sanitized = min(requested, max_valid_stop)
        return sanitized, quote.bid, "below_bid_buffer"

    async def _emit_lifecycle_transition(
        self,
        transition: LifecycleTransition | None,
        *,
        reason: str,
    ) -> None:
        if transition is None:
            return
        payload = {
            "symbol": transition.symbol,
            "deployment_id": transition.deployment_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_state": transition.previous_state.value if transition.previous_state else None,
            "new_state": transition.new_state.value,
            "option_symbol": transition.option_symbol,
            "order_id": transition.order_id,
            "reason": reason,
        }
        await self.event_repository.append("lifecycle_transition", payload)
        if self.event_bus is not None:
            await self.event_bus.publish(
                TradeLifecycleTransitionEvent(
                    symbol=transition.symbol,
                    deployment_id=transition.deployment_id,
                    timestamp=datetime.now(UTC),
                    previous_state=transition.previous_state.value if transition.previous_state else None,
                    new_state=transition.new_state.value,
                    option_symbol=transition.option_symbol,
                    order_id=transition.order_id,
                    reason=reason,
                )
            )

    async def _record_manual_status(
        self,
        deployment: DeploymentManifest,
        *,
        stage: str,
        writer_call,
    ) -> None:
        if writer_call is None:
            return
        error = await writer_call
        if error is None:
            return
        await self.event_repository.append(
            "sheet_status_writeback_failure",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "stage": stage,
                "error": error,
            },
        )

    async def _upsert_trade_record(self, record: TradeRecord) -> None:
        await self.trade_state_repository.upsert_trade(record)

    async def _finalize_cash_guard_reservation(self, trade_id: str) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.finalize_entry(trade_id)

    async def _release_cash_guard_reservation(self, trade_id: str) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.release_entry(trade_id)

    async def _paper_exit_fill_details(self, position: TrackedPosition, *, order_id: str) -> dict[str, Any]:
        exit_price = None
        payload = None
        status = "FILLED"
        if position.option_symbol:
            quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
            exit_price = quote.exit_reference_price
            payload = {
                "source": "paper_shadow" if position.source == "shadow" else "dry_run",
                "symbol": position.option_symbol,
                "bid": quote.bid,
                "ask": quote.ask,
                "last": quote.last,
                "spread_pct": quote.spread_pct,
                "averagePrice": exit_price,
                "filledQuantity": position.quantity,
                "closedAt": datetime.now(UTC).isoformat(),
                "status": status,
                "type": "PAPER",
            }
        return {
            "exit_order_id": order_id,
            "exit_price": exit_price,
            "exit_filled_quantity": position.quantity if exit_price is not None else None,
            "exit_filled_at": datetime.now(UTC) if exit_price is not None else None,
            "exit_order_status": status if exit_price is not None else None,
            "exit_order_type": "PAPER" if exit_price is not None else None,
            "exit_broker_payload": payload,
        }

    async def _emit_shadow_exit_assumed(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        fill_details: dict[str, Any],
        *,
        reason: list[str],
    ) -> None:
        exit_price = fill_details.get("exit_price")
        await self.event_repository.append(
            "shadow_exit_assumed",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "realized_pnl_usd": _premium_pnl(position.entry_price, exit_price, position.quantity),
                "realized_stop_r": _realized_stop_r(
                    position.entry_price,
                    exit_price,
                    deployment.exit.stop_loss_pct or deployment.risk.stop_loss_pct,
                ),
                "exit_order_id": fill_details.get("exit_order_id"),
                "reason": list(reason),
            },
        )

    async def _sync_cash_guard(self) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.sync_positions(
            self.planner.position_tracker.active_positions(),
            await self.trade_state_repository.get_open_trades(),
        )


def _profile_owns_profit_taking(deployment: DeploymentManifest) -> bool:
    """OPERATOR RULE (2026-07-02, non-overridable by config): when the profile-exit
    route owns a deployment's exits (armed live), the runtime must NOT ALSO arm a
    full-size profit target — neither a resting broker target order nor the
    virtual-target machinery. The profile ladder owns ALL profit-taking (T1
    partial at 1R via dispatch, breakeven, T2 runner, giveback, no_progress,
    eod_flat). The resting protective STOP is untouched by this rule.

    Motivation: on 2026-07-02 NVDA and AMD carried both a full-size resting
    target at ``option_profit_target_pct`` (+35%) AND an armed profile ladder.
    The resting target filled broker-side before the ladder could bank its T1
    partial, so both positions exited 100% at 1R and the T2 runner never had a
    chance to play out. This predicate is the single deployment-level gate that
    stops that recurrence.

    Probes the SAME fail-closed dispatch gate the armed profile-exit route uses
    (``profile_exit_dispatch_allowed``), with ``position_source`` plugged as the
    live-entry value (``"live_open"``) — this is a DEPLOYMENT-level capability
    check ("if this deployment opened a live position right now, would the
    profile route be authoritative over it"), not a read of any actual open
    position's source.

    Because the probe hardcodes a permissive ``position_source``, it cannot rely
    on the real position-source gate to keep a shadow-only lane closed. A
    shadow-only deployment (``deployment.execution.shadow_only`` — e.g. the MU
    lane, which may still carry ``exit.profile_exit_drives_live=true`` in the
    sheet) structurally never opens a ``live_open`` position (see
    ``app/runtime.py``'s ``simulate_only = deployment.execution.shadow_only`` ->
    forced ``dry_run`` -> ``planner.plan_entry`` never yields a broker
    ``order_id`` -> ``handle_signal`` takes the ``simulate_only`` branch,
    ``source="shadow"``, never ``_protect_live_entry``). So this predicate
    fails closed on ``deployment.execution.shadow_only`` explicitly, in addition
    to probing the gate, keeping the target machinery unchanged for shadow-only
    lanes regardless of the sheet's live-drive flag.
    """
    if not ExecutionSupervisor._deployment_carries_exit_profile(deployment):
        return False
    if deployment.execution.shadow_only:
        return False
    drives_live = ExecutionSupervisor._profile_exit_drives_live(deployment)
    return profile_exit_dispatch_allowed(
        live=drives_live,
        deployment_shadow_only=not drives_live,
        position_source="live_open",
        runtime_mode=ExecutionSupervisor._resolved_runtime_mode(deployment),
    )


def _profit_target_would_be_configured_absent_profile_rule(deployment: DeploymentManifest) -> bool:
    """The raw target-configured check, ignoring the profile-owns-profit-taking
    rule. Used ONLY to detect (for observability) that the operator rule is the
    reason a target was suppressed, not merely that the deployment never asked
    for a target in the first place.
    """
    return bool(
        deployment.exit.use_profit_target
        and (
            deployment.exit.option_profit_target_pct is not None
            or deployment.exit.profit_target_multiple is not None
        )
    )


def _profit_target_configured(deployment: DeploymentManifest) -> bool:
    if _profile_owns_profit_taking(deployment):
        return False
    return _profit_target_would_be_configured_absent_profile_rule(deployment)


def _deployment_target_price(deployment: DeploymentManifest, entry_price: float) -> float:
    return _target_price(
        entry_price,
        deployment.exit.stop_loss_pct,
        deployment.exit.profit_target_multiple,
        option_profit_target_pct=deployment.exit.option_profit_target_pct,
    )


def _target_price(
    entry_price: float,
    stop_loss_pct: float,
    r_multiple: float | None,
    *,
    option_profit_target_pct: float | None = None,
) -> float:
    if option_profit_target_pct is not None:
        return entry_price * (1.0 + option_profit_target_pct)
    return entry_price * (1.0 + stop_loss_pct * (r_multiple or 0.0))


def _max_valid_sell_stop_price(bid: float) -> float | None:
    if bid <= 0.01:
        return None
    candidate = math.floor(((bid - 0.01) + 1e-9) * 100.0) / 100.0
    if candidate <= 0:
        return None
    return round_price(candidate)


def _entry_reprice_enabled(app_config: AppConfig, deployment: DeploymentManifest) -> bool:
    lane_value = deployment.execution.entry_reprice_enabled
    return app_config.entry_reprice_enabled if lane_value is None else lane_value


def _entry_reprice_cancel_after_seconds(app_config: AppConfig, deployment: DeploymentManifest) -> int:
    lane_value = deployment.execution.entry_reprice_cancel_after_seconds
    return max(int(app_config.entry_reprice_cancel_after_seconds if lane_value is None else lane_value), 0)


def _entry_reprice_checkpoints(app_config: AppConfig, deployment: DeploymentManifest) -> list[int]:
    cancel_after = _entry_reprice_cancel_after_seconds(app_config, deployment)
    lane_values = deployment.execution.entry_reprice_checkpoints_seconds
    source = app_config.entry_reprice_checkpoints_seconds if lane_values is None else lane_values
    checkpoints = sorted({max(int(value), 0) for value in source})
    return [value for value in checkpoints if value < cancel_after]


def _entry_reprice_spread_pct(app_config: AppConfig, attempt: int) -> float:
    values = [float(value) for value in app_config.entry_reprice_spread_pcts if float(value) >= 0]
    if not values:
        return 1.0
    index = max(attempt - 1, 0)
    return values[index] if index < len(values) else values[-1]


def _entry_reprice_spread_fraction(deployment: DeploymentManifest, attempt: int) -> float | None:
    values = deployment.execution.entry_reprice_spread_fractions
    if not values:
        return None
    index = max(attempt - 1, 0)
    return float(values[index] if index < len(values) else values[-1])


def _risk_open_interest_percentile(plan: TradePlan) -> float | None:
    value = plan.risk_details.get("open_interest_percentile")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _remaining_seconds(started_at: datetime, target_elapsed_seconds: int) -> int:
    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    return max(int(target_elapsed_seconds - elapsed), 0)


def _terminal_entry_error(error: str | None) -> bool:
    return str(error or "").upper() in {"REJECTED", "CANCELED", "EXPIRED"}


def _filled_entry_price(payload: dict | None, *, fallback: float) -> float:
    if not payload:
        return fallback
    for key in ("averageFillPrice", "averagePrice", "filledPrice", "price"):
        parsed = _maybe_float(payload.get(key))
        if parsed is not None:
            return parsed
    return fallback


def _confirmed_entry_fill_facts(
    payload: dict | None,
) -> tuple[float | None, int | None, datetime | None]:
    """Strict experiment facts; unlike trading, never substitute plan estimates."""
    if not payload:
        return None, None, None
    if str(payload.get("status") or "").upper() != "FILLED":
        return None, None, None
    price = None
    for key in ("averageFillPrice", "averagePrice"):
        price = _maybe_float(payload.get(key))
        if price is not None:
            break
    quantity = _maybe_int(payload.get("filledQuantity"))
    # Public's live FILLED order payload calls its order-completion timestamp
    # ``closedAt`` (verified oldmac 2026-07-10). Prefer an explicit filledAt if
    # the provider adds it; accept closedAt only under the FILLED status gate.
    filled_at = _maybe_datetime(payload.get("filledAt") or payload.get("closedAt"))
    return price, quantity, filled_at.astimezone(UTC) if filled_at is not None else None


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _maybe_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_filled_exit_order(payload: dict[str, Any], *, status: str | None, option_symbol: str | None) -> bool:
    normalized_status = str(status or payload.get("status") or "").upper()
    if normalized_status != "FILLED":
        return False
    return _matches_exit_order_identity(payload, option_symbol=option_symbol)


def _is_dead_exit_order_with_fill(payload: dict[str, Any], *, status: str | None, option_symbol: str | None) -> bool:
    """Audit finding 2 (2026-07-09): a terminally dead exit order
    (CANCELED/EXPIRED/REJECTED) that reports a fill (price AND quantity, the
    enrichment sweep's ``has_fill`` rule) still carries real exit truth --
    the lot(s) that filled before the order died. Used by the closed-truth
    retry (``_find_terminal_exit_order_payload``) so a trade finalized from a
    dead payload published before ``averagePrice`` was available can be
    re-enriched instead of keeping ``exit_price`` NULL forever."""
    normalized_status = str(status or payload.get("status") or "").upper()
    if normalized_status not in _EXIT_ORDER_DEAD_STATUSES:
        return False
    details = _exit_fill_details(payload, status=status)
    if details["exit_price"] is None or not details["exit_filled_quantity"]:
        return False
    return _matches_exit_order_identity(payload, option_symbol=option_symbol)


def _matches_exit_order_identity(payload: dict[str, Any], *, option_symbol: str | None) -> bool:
    """Shared identity check: a SELL/CLOSE order for the expected contract."""
    side = str(payload.get("side") or payload.get("orderSide") or "").upper()
    open_close = str(payload.get("openCloseIndicator") or "").upper()
    instrument_symbol = normalize_option_symbol(str((payload.get("instrument") or {}).get("symbol", "")))
    if option_symbol is not None and instrument_symbol and instrument_symbol != normalize_option_symbol(option_symbol):
        return False
    return side == "SELL" and open_close == "CLOSE"


def _exit_fill_details(payload: dict | None, *, status: str | None) -> dict[str, Any]:
    if payload is None:
        return {
            "exit_price": None,
            "exit_filled_quantity": None,
            "exit_filled_at": None,
            "exit_order_status": status,
            "exit_order_type": None,
            "exit_broker_payload": None,
        }
    return {
        "exit_price": _maybe_float(payload.get("averagePrice")),
        "exit_filled_quantity": _maybe_int(payload.get("filledQuantity")),
        "exit_filled_at": _maybe_datetime(payload.get("closedAt")),
        "exit_order_status": status or payload.get("status"),
        "exit_order_type": payload.get("type"),
        "exit_broker_payload": payload,
    }


def _premium_pnl(entry_price: float | None, exit_price: float | None, quantity: int | None) -> float | None:
    if entry_price is None or exit_price is None or not quantity:
        return None
    return round((exit_price - entry_price) * int(quantity) * 100.0, 2)


def _realized_stop_r(entry_price: float | None, exit_price: float | None, stop_loss_pct: float | None) -> float | None:
    if entry_price is None or exit_price is None or stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    risk_per_contract = entry_price * stop_loss_pct
    if risk_per_contract <= 0:
        return None
    return round((exit_price - entry_price) / risk_per_contract, 4)


def _underlying_entry_price(decision: SignalDecision) -> float | None:
    value = decision.features.get("close")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_plan_approved(plan: TradePlan) -> bool:
    return plan.risk_reasons == ["approved"]


def _target_handoff_activation_floor(
    position: TrackedPosition,
    *,
    target_approach_offset_pct: float | None,
) -> float | None:
    if position.target_price is None or target_approach_offset_pct is None:
        return None
    return position.target_price * (1.0 - target_approach_offset_pct)


def _target_handoff_high_price(
    position: TrackedPosition,
    reference_price: float,
    *,
    target_approach_offset_pct: float | None,
) -> float:
    candidates = [
        reference_price,
        position.target_activation_price,
        position.target_activation_high_price,
        _target_handoff_activation_floor(position, target_approach_offset_pct=target_approach_offset_pct),
    ]
    return max(float(value) for value in candidates if value is not None)


def _target_handoff_restore_threshold_price(entry_price: float, high_price: float, progress_pct: float) -> float:
    progress = max(0.0, min(1.0, progress_pct))
    high = max(entry_price, high_price)
    return entry_price + ((high - entry_price) * progress)


def _restore_threshold_price(entry_price: float, target_price: float, progress_pct: float) -> float:
    return _target_handoff_restore_threshold_price(entry_price, target_price, progress_pct)


def _tracked_trade_status(position: TrackedPosition) -> str:
    if position.exit_mode is not None or position.exit_order_id is not None or position.exit_submitted_at is not None:
        return "exit_pending"
    if position.target_order_id:
        return "target_active"
    return "open_protected" if position.stop_order_id else "open_unprotected"


def _profile_state_identity_mismatch(
    state: ProfileExitState,
    *,
    entry_premium: float,
) -> bool:
    """True when a cached ladder state clearly belongs to a DIFFERENT fill.

    Backstop behind reconciliation's trade matching (audit fix 2026-07-02).
    Conservative signals only, so ordinary broker jitter and the ROUTINE
    post-partial state never trip it (re-audit blocker: comparing banked
    quantity against the CURRENT position quantity fired on every tick after
    a normal T1 partial — the position then holds only the residual — and
    reseeding refired T1 and closed the T2 runner):
      * seed entry premium diverges >10% relative — same-fill entry premium is
        fixed at entry; a 0-2 DTE re-entry days later diverges far more;
      * the ladder claims more banked quantity than the ORIGINAL seeded
        quantity (impossible for the same fill at any partial stage).
    """
    seed = state.seed_entry_premium
    if seed is not None and entry_premium > 0 and seed > 0:
        if abs(seed - entry_premium) > 0.10 * max(seed, entry_premium):
            return True
    if state.seed_quantity is not None and state.banked_quantity > state.seed_quantity:
        return True
    return False


def _is_paper_order_id(order_id: str | None) -> bool:
    return bool(order_id and (order_id == "SHADOW_ENTRY" or order_id.startswith("DRY_RUN")))


def _is_paper_trade_record(trade: TradeRecord) -> bool:
    return any(
        _is_paper_order_id(order_id)
        for order_id in (
            trade.entry_order_id,
            trade.stop_order_id,
            trade.target_order_id,
            trade.exit_order_id,
        )
    )


def _paper_trade_source(trade: TradeRecord) -> str:
    return "shadow" if trade.entry_order_id == "SHADOW_ENTRY" else "dry_run"


def _resolved_recovery_stop_loss_pct(deployment: DeploymentManifest) -> tuple[float | None, str]:
    if deployment.exit.stop_loss_pct is not None and deployment.exit.stop_loss_pct > 0:
        return deployment.exit.stop_loss_pct, "deployment_native"
    if deployment.risk.stop_loss_pct is not None and deployment.risk.stop_loss_pct > 0:
        return deployment.risk.stop_loss_pct, "global_fallback"
    # HIGH-2: a profile-exit deployment must never no-op the re-arm path into a
    # naked ride. When neither the deployment nor the global stop pct is set, let
    # the profile supply its OWN recovery floor from its premium-stop dials
    # (initial stop, else the wider disaster stop). Config validation
    # (``_validate_profile_recovery_stop``) also rejects a profile deployment that
    # leaves all of these unset, so this is belt-and-suspenders for the runtime.
    exit_spec = deployment.exit
    if getattr(exit_spec, "profile_exit_id", None):
        initial = getattr(exit_spec, "initial_stop_pct", None)
        if initial is not None and initial > 0:
            return float(initial), "profile_initial_stop"
        disaster = getattr(exit_spec, "premium_disaster_stop_pct", None)
        if disaster is not None and disaster > 0:
            return float(disaster), "profile_disaster_stop"
    return None, "unavailable"


class _DryRunOrderResult:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.error = None


def _is_self_disarming_manual_deployment(deployment: DeploymentManifest) -> bool:
    metadata = deployment.source.metadata or {}
    return (
        deployment.source.origin == "active_sheet_manual"
        and metadata.get("row_index") is not None
        and metadata.get("sheet_name") is not None
    )


def _hard_flat_market_fallback_due(
    exit_submitted_at: datetime | None,
    now: datetime,
    deployment: DeploymentManifest,
) -> bool:
    if exit_submitted_at is None:
        return False
    if now >= exit_submitted_at + timedelta(seconds=10):
        return True
    hard_flat_time = parse_time_text(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")
    now_seconds = as_et_time(now).hour * 3600 + as_et_time(now).minute * 60 + as_et_time(now).second
    hard_flat_seconds = hard_flat_time.hour * 3600 + hard_flat_time.minute * 60 + 30
    return now_seconds >= hard_flat_seconds


def _material_exit_price_change(previous_price: float | None, next_price: float | None) -> bool:
    if next_price is None:
        return False
    if previous_price is None:
        return True
    return round(previous_price, 2) != round(next_price, 2)


def _replace_position(position: TrackedPosition, **changes) -> TrackedPosition:
    return replace(position, **changes)


def _decision_is_partial_scale(decision: ExitDecision) -> bool:
    """True when an exit decision asks for a partial scale (reduce, not flatten)."""
    return bool(decision.features.get("partial_scale"))


def _residual_protective_stop_price(
    position: TrackedPosition,
    decision: ExitDecision,
    deployment: DeploymentManifest,
    *,
    prior_stop_price: float | None = None,
) -> float | None:
    """Resolve a protective stop price for the residual after a profile partial.

    NEW-1: a profile partial must ALWAYS leave the residual protected, even when
    the position carried no prior resting stop (``stop_price is None``). This
    derives a residual stop from, in order of preference:

      1. the prior resting stop price — unchanged behavior when a stop was already
         in place. ``prior_stop_price`` is the price captured by the caller BEFORE
         it cancelled protection (MEDIUM-1: ``_cancel_exit_protection`` now clears
         ``stop_price`` when it clears the stop order, so the live ``position``
         passed here may already have ``stop_price=None``); falls back to
         ``position.stop_price`` when the caller does not supply it;
      2. the profile's own initial premium stop, recovered from the decision's
         diagnostics (``entry_premium - risk_per_contract`` — i.e. ``entry *
         (1 - initial_stop_pct)``), so the residual inherits the profile's stop;
      3. the deployment exit spec's ``initial_stop_pct``/``stop_loss_pct`` applied
         to the position entry price as a final fallback.

    Returns ``None`` only when no positive entry/stop information exists anywhere,
    which the caller treats as a hard error rather than going naked.
    """
    resting_stop = prior_stop_price if prior_stop_price is not None else position.stop_price
    if resting_stop is not None and resting_stop > 0:
        return float(resting_stop)

    features = decision.features or {}
    entry_premium = features.get("entry_premium")
    risk_per_contract = features.get("risk_per_contract")
    try:
        if entry_premium is not None and risk_per_contract is not None:
            derived = float(entry_premium) - float(risk_per_contract)
            if derived > 0:
                return round(derived, 4)
    except (TypeError, ValueError):
        pass

    entry_price = position.entry_price
    if entry_price is not None and entry_price > 0:
        exit_spec = getattr(deployment, "exit", None)
        stop_pct = None
        if exit_spec is not None:
            stop_pct = getattr(exit_spec, "initial_stop_pct", None)
            if stop_pct is None or stop_pct <= 0:
                stop_pct = getattr(exit_spec, "stop_loss_pct", None)
        if stop_pct and stop_pct > 0:
            derived = float(entry_price) * (1.0 - float(stop_pct))
            if derived > 0:
                return round(derived, 4)
    return None


def _resolve_exit_quantity(decision: ExitDecision, position: TrackedPosition) -> int:
    """Resolve how many contracts an exit decision should close.

    C1: honor ``features["exit_quantity"]`` when present (the profile evaluator
    sets it for staged/partial exits). Falls back to the full position quantity
    for plain full exits that carry no quantity.

    Hard guard: a PARTIAL_SCALE (``features["partial_scale"]``) can NEVER close
    the full position — the residual runner must stay open. A partial quantity is
    clamped to ``[1, position.quantity - 1]``; if the evaluator somehow asked a
    partial to flatten (>= full qty), we raise rather than silently flattening.
    """
    requested = decision.features.get("exit_quantity")
    is_partial = _decision_is_partial_scale(decision)
    if requested is None:
        if is_partial:
            raise ValueError(
                "partial_scale exit decision carried no exit_quantity; refusing to "
                "flatten the full position on a partial"
            )
        return position.quantity
    try:
        qty = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid exit_quantity {requested!r}") from exc
    if qty <= 0:
        raise ValueError(f"exit_quantity must be positive, got {qty}")
    if is_partial:
        if position.quantity <= 1:
            raise ValueError(
                "partial_scale requested but position has <= 1 contract; a partial "
                "cannot leave a residual runner"
            )
        if qty >= position.quantity:
            raise ValueError(
                f"partial_scale exit_quantity ({qty}) >= position quantity "
                f"({position.quantity}); a PARTIAL_SCALE must never close the full "
                "position"
            )
        return qty
    # Full exit: never close more than the position holds.
    return min(qty, position.quantity)
