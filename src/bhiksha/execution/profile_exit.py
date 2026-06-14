"""Profile-aware exit evaluator (operator exit-profile ladder, premium-anchored).

This is the Bhiksha runtime counterpart to the offline calibration policy
``mala_v2/src/oracle/trade_simulator.py:ProfileExitPolicy``. Where the offline
policy walks *underlying* bars, this evaluator runs the same deterministic
priority ladder on the **live option premium** (entry premium, current bid/mid,
elapsed wall-clock time, optionally greeks). It is the executable consumer of the
kernel v2 :class:`ManagementPolicySpec` exit-profile fields.

Design constraints (enforced by the mission):

* **No new broker/order paths.** Every decision produced here maps onto an
  *existing* Bhiksha FSM action — see :class:`ProfileFsmAction` and
  :func:`profile_decision_to_exit_decision`. The evaluator never talks to a
  broker, order manager, or position tracker; it is a pure function of
  (profile, entry premium, current quote, elapsed time, prior ladder state).
* **Shadow-first.** The evaluator only *computes* decisions. Whether a decision
  is dispatched to the broker or merely recorded is decided one layer up via
  :func:`profile_exit_dispatch_allowed` and the shadow recorder in
  ``profile_exit_shadow.py``. The default is record-only.

The priority ladder (highest priority first), identical in ordering to the
offline policy::

    1. initial premium stop  /  premium disaster stop   -> full square_off
    2. target-1 partial (bank ``target_1_quantity``)    -> partial square_off,
       then ratchet the stop to breakeven                  then stop_to_breakeven
    3. target-2 runner (remaining quantity)             -> full square_off
    4. high-water giveback (armed by peak R)            -> full square_off
    5. no-progress time stop  /  max-hold time stop     -> full square_off
    6. EOD hard flat (when eod_flat)                    -> full square_off (hard)

``R`` is defined by the **premium stop distance**: ``R = initial_stop_pct *
entry_premium`` (falling back to ``option_stop_fallback_pct`` /
``premium_disaster_stop_pct`` when ``initial_stop_pct`` is unset). A long option
position is *always* "long premium" — premium up is favorable, premium down is
adverse — so unlike the underlying policy there is no separate short branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone
from enum import Enum
from typing import Any

UTC = timezone.utc

# High-water giveback tiers mirror kernel ``GIVEBACK_POLICIES``. Each tier maps to
# (arm_r, retrace_frac): arm once peak favorable excursion reaches arm_r (in R),
# then exit when premium gives back retrace_frac of that peak.  OFF disables.
GIVEBACK_TIERS: dict[str, tuple[float, float]] = {
    "OFF": (0.0, 0.0),
    "STRICT": (1.0, 0.33),
    "MODERATE": (1.25, 0.50),
    "LOOSE": (1.5, 0.66),
}

_DEFAULT_EOD_TIME = dt_time(15, 55)


class ProfileLadderRule(str, Enum):
    """Which rung of the priority ladder fired (or HOLD)."""

    HOLD = "hold"
    INITIAL_STOP = "initial_stop"
    DISASTER_STOP = "disaster_stop"
    TARGET_1_PARTIAL = "target_1_partial"
    TARGET_2_RUNNER = "target_2_runner"
    HIGH_WATER_GIVEBACK = "high_water_giveback"
    NO_PROGRESS = "no_progress"
    MAX_HOLD = "max_hold"
    EOD_FLAT = "eod_flat"


class ProfileFsmAction(str, Enum):
    """Existing Bhiksha FSM actions a ladder rule can map to.

    These are *not* new actions — they name the broker-affecting primitives the
    supervisor already implements:

    * ``HOLD``            — no action (ExitDecision.exit == False).
    * ``SQUARE_OFF``      — full-position close with protection cancel; handled by
      ``supervisor._handle_exit_locked`` (action == "square_off").
    * ``PARTIAL_SCALE``   — close a *fraction* of the position; handled by the
      existing ``order_manager.place_close_order(option_symbol, quantity, ...)``
      with a reduced quantity. Represented as a square_off carrying
      ``exit_quantity`` < position quantity.
    * ``STOP_TO_BREAKEVEN`` — cancel the live stop and place a new stop at entry;
      this is exactly ``supervisor``'s existing breakeven-promotion branch
      (``place_stop_loss_order(option_symbol, entry_price, qty)``).
    * ``HARD_FLAT``       — EOD/forced flat; maps to a square_off with the
      EMERGENCY/HARD_FLAT exit mode the supervisor already uses for EOD.
    """

    HOLD = "hold"
    SQUARE_OFF = "square_off"
    PARTIAL_SCALE = "partial_scale"
    STOP_TO_BREAKEVEN = "stop_to_breakeven"
    HARD_FLAT = "hard_flat"


@dataclass(slots=True, frozen=True)
class ProfileExitFields:
    """The v2 exit-profile dials, lifted from a kernel ManagementPolicySpec.

    Kept as a plain dataclass (not a pydantic model) so the evaluator has no hard
    dependency on the kernel import at call time; :meth:`from_management_spec`
    and :meth:`from_exit_params` adapt the two real sources.
    """

    profile_id: str
    target_1_r: float | None = None
    target_2_r: float | None = None
    target_1_quantity: float = 1.0
    initial_stop_pct: float | None = None
    premium_disaster_stop_pct: float | None = None
    option_stop_fallback_pct: float = 0.45
    target_r: float = 0.0
    no_progress_seconds: int | None = None
    max_hold_seconds: int | None = None
    high_water_giveback_policy: str = "OFF"
    breakeven_after_t1: bool = True
    eod_flat: bool = True
    hard_flat_time_et: str = "15:55"

    @property
    def stop_pct(self) -> float:
        """Premium-% stop distance that defines R.

        Prefers ``initial_stop_pct``; falls back to the disaster stop, then to
        ``option_stop_fallback_pct``. Always > 0 so R is well-defined.
        """
        for candidate in (self.initial_stop_pct, self.premium_disaster_stop_pct, self.option_stop_fallback_pct):
            if candidate is not None and candidate > 0:
                return float(candidate)
        return 0.45

    @property
    def disaster_stop_pct(self) -> float | None:
        """Wider catastrophe backstop, if distinct from the initial stop."""
        if self.premium_disaster_stop_pct is not None and self.premium_disaster_stop_pct > 0:
            return float(self.premium_disaster_stop_pct)
        return None

    @property
    def giveback_arm_r(self) -> float | None:
        arm, _ = GIVEBACK_TIERS.get(self.high_water_giveback_policy.upper(), (0.0, 0.0))
        return arm if arm > 0 else None

    @property
    def giveback_retrace_frac(self) -> float:
        _, frac = GIVEBACK_TIERS.get(self.high_water_giveback_policy.upper(), (0.0, 0.5))
        return frac if frac > 0 else 0.5

    @property
    def effective_target_1_r(self) -> float | None:
        if self.target_1_r is not None and self.target_1_r > 0:
            return float(self.target_1_r)
        # Pre-v2 single-target spec: target_r acts as the lone target.
        if self.target_r > 0 and (self.target_2_r is None):
            return None
        return None

    @property
    def effective_target_2_r(self) -> float | None:
        if self.target_2_r is not None and self.target_2_r > 0:
            return float(self.target_2_r)
        if self.target_r > 0:
            return float(self.target_r)
        return None

    @classmethod
    def from_management_spec(cls, spec: Any) -> "ProfileExitFields":
        """Adapt a kernel ``ManagementPolicySpec`` (v2) into evaluator fields.

        Accepts either the pydantic model or a plain dict (``model_dump``) so the
        kernel import stays optional here.
        """
        get = spec.get if isinstance(spec, dict) else (lambda k, d=None: getattr(spec, k, d))
        return cls(
            profile_id=str(get("policy_id", "")) or "unknown_profile",
            target_1_r=get("target_1_r"),
            target_2_r=get("target_2_r"),
            target_1_quantity=float(get("target_1_quantity", 1.0) or 1.0),
            initial_stop_pct=get("initial_stop_pct"),
            premium_disaster_stop_pct=get("premium_disaster_stop_pct"),
            option_stop_fallback_pct=float(get("option_stop_fallback_pct", 0.45) or 0.45),
            target_r=float(get("target_r", 0.0) or 0.0),
            no_progress_seconds=get("no_progress_seconds"),
            max_hold_seconds=get("max_hold_seconds"),
            high_water_giveback_policy=str(get("high_water_giveback_policy", "OFF") or "OFF"),
            breakeven_after_t1=bool(get("breakeven_after_t1", True)),
            eod_flat=bool(get("eod_flat", True)),
            hard_flat_time_et=str(get("hard_flat_time_et", "15:55") or "15:55"),
        )

    @classmethod
    def from_exit_spec(cls, exit_spec: Any) -> "ProfileExitFields":
        """Adapt a Bhiksha ``ExitSpec`` (with v2 profile dials) into evaluator fields.

        This is the deployment-side entry point: an operator pins a frozen named
        profile on a deployment's exit spec (``profile_exit_id`` + the v2 dials)
        and the evaluator reads them straight off the spec. Accessed via getattr
        so the evaluator keeps no hard import of the config layer.
        """
        get = lambda k, d=None: getattr(exit_spec, k, d)  # noqa: E731
        return cls(
            profile_id=str(get("profile_exit_id") or get("profile") or "unknown_profile"),
            target_1_r=get("target_1_r"),
            target_2_r=get("target_2_r"),
            target_1_quantity=float(get("target_1_quantity", 1.0) or 1.0),
            initial_stop_pct=get("initial_stop_pct"),
            premium_disaster_stop_pct=get("premium_disaster_stop_pct"),
            option_stop_fallback_pct=float(get("stop_loss_pct", 0.45) or 0.45),
            target_r=float(get("profit_target_multiple", 0.0) or 0.0),
            no_progress_seconds=get("no_progress_seconds"),
            max_hold_seconds=get("max_hold_seconds"),
            high_water_giveback_policy=str(get("high_water_giveback_policy", "OFF") or "OFF"),
            breakeven_after_t1=bool(get("breakeven_after_t1", True)),
            eod_flat=bool(get("eod_flat", True)),
            hard_flat_time_et=str(get("hard_flat_time_et", "15:55") or "15:55"),
        )

    @classmethod
    def from_exit_params(cls, profile_id: str, params: dict[str, Any], *, fallback_stop_pct: float = 0.45) -> "ProfileExitFields":
        """Adapt a Bhiksha ``ExitSpec.thesis_exit_params`` dict into evaluator fields.

        Lets an operator pin a frozen named profile directly on a deployment's
        exit spec (``thesis_exit_policy: profile_premium``) without round-tripping
        through a packet. Field names mirror the kernel spec.
        """

        def num(key: str) -> float | None:
            value = params.get(key)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def integer(key: str) -> int | None:
            value = params.get(key)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            profile_id=profile_id,
            target_1_r=num("target_1_r"),
            target_2_r=num("target_2_r"),
            target_1_quantity=num("target_1_quantity") or 1.0,
            initial_stop_pct=num("initial_stop_pct"),
            premium_disaster_stop_pct=num("premium_disaster_stop_pct"),
            option_stop_fallback_pct=num("option_stop_fallback_pct") or fallback_stop_pct,
            target_r=num("target_r") or 0.0,
            no_progress_seconds=integer("no_progress_seconds"),
            max_hold_seconds=integer("max_hold_seconds"),
            high_water_giveback_policy=str(params.get("high_water_giveback_policy", "OFF") or "OFF"),
            breakeven_after_t1=bool(params.get("breakeven_after_t1", True)),
            eod_flat=bool(params.get("eod_flat", True)),
            hard_flat_time_et=str(params.get("hard_flat_time_et", "15:55") or "15:55"),
        )


@dataclass(slots=True)
class ProfileExitState:
    """Per-trade ladder state carried across evaluations.

    Mirrors ``ProfileExitPolicy._state_by_entry_idx`` but keyed by the caller.
    The evaluator never mutates a position; it mutates only this state object so
    the ladder is deterministic and replayable. ``peak_premium`` is updated only
    *after* the rule checks each tick (so a single tick cannot both arm and fire
    the giveback rule — matching the offline policy).
    """

    peak_premium: float
    target_1_banked: bool = False
    target_1_premium: float | None = None
    stop_at_breakeven: bool = False
    banked_quantity: int = 0
    breakeven_emitted: bool = False

    @classmethod
    def new(cls, entry_premium: float) -> "ProfileExitState":
        return cls(peak_premium=entry_premium)

    def target_1_banked_stop_emitted(self) -> bool:
        """True once the stop-to-breakeven FSM action has been surfaced."""
        return self.breakeven_emitted

    def mark_breakeven_emitted(self) -> None:
        self.breakeven_emitted = True


@dataclass(slots=True, frozen=True)
class ProfileExitDecision:
    """Outcome of one ladder evaluation: which rule fired and its FSM mapping."""

    profile_id: str
    rule: ProfileLadderRule
    fsm_action: ProfileFsmAction
    reason: str
    exit: bool
    # For PARTIAL_SCALE: number of contracts to bank now. For full exits: full qty.
    exit_quantity: int = 0
    # For STOP_TO_BREAKEVEN: the new stop premium to place (== entry premium).
    target_stop_price: float | None = None
    # Whether protection (resting stop/target) must be cancelled before the exit.
    cancel_protection_orders: bool = False
    # Diagnostics for shadow records / event logs.
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        return self.fsm_action is ProfileFsmAction.PARTIAL_SCALE


@dataclass(slots=True, frozen=True)
class ProfileMarketView:
    """Minimal live-premium snapshot the evaluator needs.

    ``current_premium`` is the *exit reference* premium (bid, then last, then
    ask) — what the position would realize if flattened now. The caller derives
    it from an ``order_manager.PublicQuote.exit_reference_price``.
    """

    current_premium: float | None
    bar_time_et: dt_time | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None


def evaluate_profile_exit(
    *,
    fields: ProfileExitFields,
    entry_premium: float,
    quantity: int,
    market: ProfileMarketView,
    entry_time: datetime | None,
    now: datetime | None = None,
    state: ProfileExitState,
) -> ProfileExitDecision:
    """Run the deterministic priority ladder on the live option premium.

    Returns a :class:`ProfileExitDecision`. ``state`` is mutated in place to
    carry the ladder forward (peak premium, T1-banked flag, breakeven flag).
    """

    profile_id = fields.profile_id
    now = now or datetime.now(UTC)

    if entry_premium <= 0 or quantity <= 0:
        return _hold(profile_id, reason="profile_no_position")

    current = market.current_premium
    risk = fields.stop_pct * entry_premium
    if risk <= 0:
        return _hold(profile_id, reason="profile_no_risk")

    elapsed_seconds = _elapsed_seconds(entry_time, now)

    # 0. EOD hard flat — highest precedence when enabled and we have a bar clock.
    if fields.eod_flat and market.bar_time_et is not None:
        eod = _parse_time(fields.hard_flat_time_et)
        if market.bar_time_et >= eod:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.EOD_FLAT,
                fsm_action=ProfileFsmAction.HARD_FLAT,
                reason=f"profile_eod_flat:{eod.strftime('%H%M')}",
                quantity=_remaining_quantity(quantity, state),
                features=_diag(entry_premium, current, risk, state, elapsed_seconds),
            )

    if current is None:
        # No tradeable premium this tick: only the time/EOD rules above can fire.
        return _hold(profile_id, reason="profile_premium_unavailable")

    # peak/excursion measured from PRIOR ticks only (state not yet updated).
    peak_move = state.peak_premium - entry_premium
    peak_r = peak_move / risk if peak_move > 0 else 0.0
    current_move = current - entry_premium
    current_r = current_move / risk

    remaining_qty = _remaining_quantity(quantity, state)

    # 1a. Disaster stop — absolute catastrophe floor. Checked FIRST and
    #     regardless of T1/breakeven state, because its job is to catch a
    #     gap-through that blew past the active (initial or breakeven) stop. In
    #     the operator profiles the disaster % is wider than the initial %, so it
    #     fires only when the normal stop was skipped (e.g. an overnight/illiquid
    #     gap). Distinct from option_stop_fallback_pct (the live-broker backstop).
    disaster_pct = fields.disaster_stop_pct
    if disaster_pct is not None:
        disaster_price = entry_premium * (1.0 - disaster_pct)
        if current <= disaster_price:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.DISASTER_STOP,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason="profile_premium_disaster_stop",
                quantity=remaining_qty,
                features=_diag(entry_premium, current, risk, state, elapsed_seconds),
            )

    # 1b. Active protective stop — breakeven after a banked T1, else the initial
    #     premium stop that defines R.
    if state.target_1_banked and fields.breakeven_after_t1:
        if current <= entry_premium:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.INITIAL_STOP,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason="profile_breakeven_stop",
                quantity=remaining_qty,
                features=_diag(entry_premium, current, risk, state, elapsed_seconds),
            )
    elif current <= entry_premium - risk:
        return _full_exit(
            profile_id,
            rule=ProfileLadderRule.INITIAL_STOP,
            fsm_action=ProfileFsmAction.SQUARE_OFF,
            reason="profile_initial_stop",
            quantity=remaining_qty,
            features=_diag(entry_premium, current, risk, state, elapsed_seconds),
        )

    # 2. Target 1 — bank the partial and arm the breakeven ratchet.
    t1_r = fields.effective_target_1_r
    if not state.target_1_banked and t1_r is not None and fields.target_1_quantity > 0:
        t1_price = entry_premium + t1_r * risk
        if current >= t1_price:
            bank_qty = _partial_quantity(quantity, fields.target_1_quantity)
            state.target_1_banked = True
            state.target_1_premium = current
            # Full position banked at T1 -> a plain full exit, not a partial.
            if bank_qty >= quantity or fields.target_1_quantity >= 1.0:
                state.banked_quantity = quantity
                return _full_exit(
                    profile_id,
                    rule=ProfileLadderRule.TARGET_1_PARTIAL,
                    fsm_action=ProfileFsmAction.SQUARE_OFF,
                    reason="profile_target_1_full",
                    quantity=quantity,
                    features=_diag(entry_premium, current, risk, state, elapsed_seconds),
                )
            state.banked_quantity = bank_qty
            if fields.breakeven_after_t1:
                state.stop_at_breakeven = True
            return ProfileExitDecision(
                profile_id=profile_id,
                rule=ProfileLadderRule.TARGET_1_PARTIAL,
                fsm_action=ProfileFsmAction.PARTIAL_SCALE,
                reason="profile_target_1_partial",
                exit=True,
                exit_quantity=bank_qty,
                cancel_protection_orders=True,
                features={
                    **_diag(entry_premium, current, risk, state, elapsed_seconds),
                    "bank_quantity": bank_qty,
                    "remaining_quantity": quantity - bank_qty,
                    "arms_breakeven": fields.breakeven_after_t1,
                },
            )

    # 3. Target 2 — full exit of the runner (or the lone target when no partial).
    t2_r = fields.effective_target_2_r
    runner_armed = state.target_1_banked or t1_r is None or fields.target_1_quantity <= 0
    if runner_armed and t2_r is not None:
        t2_price = entry_premium + t2_r * risk
        if current >= t2_price:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.TARGET_2_RUNNER,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason="profile_target_2_runner",
                quantity=remaining_qty,
                features=_diag(entry_premium, current, risk, state, elapsed_seconds),
            )

    # 4. High-water giveback (armed by peak R from prior ticks).
    arm_r = fields.giveback_arm_r
    if arm_r is not None and peak_r >= arm_r:
        floor_r = peak_r * (1.0 - fields.giveback_retrace_frac)
        if current_r <= floor_r:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.HIGH_WATER_GIVEBACK,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason=f"profile_high_water_giveback:{fields.high_water_giveback_policy.upper()}",
                quantity=remaining_qty,
                features={
                    **_diag(entry_premium, current, risk, state, elapsed_seconds),
                    "peak_r": round(peak_r, 4),
                    "floor_r": round(floor_r, 4),
                },
            )

    # 5. Time stops — max hold, then no-progress.
    if elapsed_seconds is not None:
        if fields.max_hold_seconds is not None and elapsed_seconds >= fields.max_hold_seconds:
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.MAX_HOLD,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason=f"profile_max_hold:{fields.max_hold_seconds}s",
                quantity=remaining_qty,
                features=_diag(entry_premium, current, risk, state, elapsed_seconds),
            )
        if (
            fields.no_progress_seconds is not None
            and elapsed_seconds >= fields.no_progress_seconds
            and peak_r < 0.25
        ):
            return _full_exit(
                profile_id,
                rule=ProfileLadderRule.NO_PROGRESS,
                fsm_action=ProfileFsmAction.SQUARE_OFF,
                reason=f"profile_no_progress:{fields.no_progress_seconds}s",
                quantity=remaining_qty,
                features={
                    **_diag(entry_premium, current, risk, state, elapsed_seconds),
                    "peak_r": round(peak_r, 4),
                },
            )

    # No rung fired. If T1 just armed the breakeven ratchet (state mutated above
    # without an immediate exit) emit the stop-to-breakeven FSM action so the
    # caller can tighten the live stop.
    if state.stop_at_breakeven and not state.target_1_banked_stop_emitted():
        state.mark_breakeven_emitted()
        return ProfileExitDecision(
            profile_id=profile_id,
            rule=ProfileLadderRule.TARGET_1_PARTIAL,
            fsm_action=ProfileFsmAction.STOP_TO_BREAKEVEN,
            reason="profile_stop_to_breakeven",
            exit=False,
            target_stop_price=entry_premium,
            features=_diag(entry_premium, current, risk, state, elapsed_seconds),
        )

    # Update the peak AFTER all checks (avoid same-tick arm+fire on giveback).
    if current > state.peak_premium:
        state.peak_premium = current
    return _hold(profile_id, reason="profile_hold", features=_diag(entry_premium, current, risk, state, elapsed_seconds))


# --------------------------------------------------------------------------- #
# Decision -> existing Bhiksha ExitDecision mapping
# --------------------------------------------------------------------------- #


def profile_decision_to_exit_decision(
    decision: ProfileExitDecision,
    *,
    deployment_id: str,
    symbol: str,
    timestamp: datetime,
) -> Any:
    """Map a :class:`ProfileExitDecision` onto bhiksha's domain ``ExitDecision``.

    Imported lazily so the evaluator module itself has no hard dependency on the
    domain layer (keeps it unit-testable in isolation).

    Mapping (decision.fsm_action -> ExitDecision.action), all existing actions:

    * HOLD / STOP_TO_BREAKEVEN -> ``action="hold"`` (the stop-adjust is applied by
      the supervisor's existing breakeven-promotion branch, *not* via an exit
      order; we surface ``replacement_stop_price`` so the caller can tighten it).
    * SQUARE_OFF / HARD_FLAT    -> ``action="square_off"`` (full close; the
      existing ``_handle_exit_locked`` square_off path).
    * PARTIAL_SCALE             -> ``action="square_off"`` carrying
      ``features["exit_quantity"]`` so the caller closes a reduced quantity via
      the existing ``place_close_order(option_symbol, quantity, ...)``.
    """
    from bhiksha.domain.models import ExitDecision  # local import by design

    action = "hold"
    if decision.fsm_action in (ProfileFsmAction.SQUARE_OFF, ProfileFsmAction.HARD_FLAT, ProfileFsmAction.PARTIAL_SCALE):
        action = "square_off"

    features = dict(decision.features)
    features["profile_id"] = decision.profile_id
    features["profile_rule"] = decision.rule.value
    features["profile_fsm_action"] = decision.fsm_action.value
    if decision.exit_quantity:
        features["exit_quantity"] = decision.exit_quantity
    if decision.is_partial:
        features["partial_scale"] = True

    return ExitDecision(
        deployment_id=deployment_id,
        symbol=symbol,
        timestamp=timestamp,
        exit=decision.exit,
        action=action,
        reason=[decision.reason],
        cancel_protection_orders=decision.cancel_protection_orders,
        replacement_stop_price=decision.target_stop_price,
        features=features,
    )


# --------------------------------------------------------------------------- #
# Shadow-vs-live gate
# --------------------------------------------------------------------------- #


def profile_exit_dispatch_allowed(
    *,
    live: bool,
    deployment_shadow_only: bool,
    position_source: str,
    runtime_mode: str | None = None,
) -> bool:
    """Decide whether a profile exit decision may be DISPATCHED to the broker.

    Returns ``True`` only when *every* live precondition holds. The default is
    record-only (shadow). This is the single chokepoint that keeps the evaluator
    shadow-first; the live runtime never bypasses it.

    Live requires ALL of:
      * ``live`` runtime flag set by the operator,
      * deployment not flagged ``shadow_only``,
      * the position is not a shadow/dry-run position,
      * runtime_mode is the kernel ``live_approval_gated`` mode (or unset for
        callers that gate elsewhere) — never ``shadow``/``advisory``.
    """
    if not live:
        return False
    if deployment_shadow_only:
        return False
    if position_source in {"shadow", "dry_run"}:
        return False
    if runtime_mode is not None and runtime_mode not in {"live_approval_gated", "live_automated"}:
        return False
    return True


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _hold(profile_id: str, *, reason: str, features: dict[str, Any] | None = None) -> ProfileExitDecision:
    return ProfileExitDecision(
        profile_id=profile_id,
        rule=ProfileLadderRule.HOLD,
        fsm_action=ProfileFsmAction.HOLD,
        reason=reason,
        exit=False,
        features=features or {},
    )


def _full_exit(
    profile_id: str,
    *,
    rule: ProfileLadderRule,
    fsm_action: ProfileFsmAction,
    reason: str,
    quantity: int,
    features: dict[str, Any],
) -> ProfileExitDecision:
    return ProfileExitDecision(
        profile_id=profile_id,
        rule=rule,
        fsm_action=fsm_action,
        reason=reason,
        exit=True,
        exit_quantity=quantity,
        cancel_protection_orders=True,
        features=features,
    )


def _remaining_quantity(quantity: int, state: ProfileExitState) -> int:
    remaining = quantity - state.banked_quantity
    return remaining if remaining > 0 else quantity


def _partial_quantity(quantity: int, fraction: float) -> int:
    if fraction >= 1.0:
        return quantity
    banked = int(round(quantity * fraction))
    banked = max(1, min(banked, quantity - 1)) if quantity > 1 else quantity
    return banked


def _elapsed_seconds(entry_time: datetime | None, now: datetime) -> float | None:
    if entry_time is None:
        return None
    entry = entry_time if entry_time.tzinfo is not None else entry_time.replace(tzinfo=UTC)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    delta = (reference - entry).total_seconds()
    return delta if delta >= 0 else 0.0


def _parse_time(value: str) -> dt_time:
    try:
        parts = value.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return _DEFAULT_EOD_TIME


def _diag(
    entry_premium: float,
    current: float | None,
    risk: float,
    state: ProfileExitState,
    elapsed_seconds: float | None,
) -> dict[str, Any]:
    current_r = ((current - entry_premium) / risk) if (current is not None and risk > 0) else None
    return {
        "entry_premium": round(entry_premium, 4),
        "current_premium": round(current, 4) if current is not None else None,
        "risk_per_contract": round(risk, 4),
        "current_r": round(current_r, 4) if current_r is not None else None,
        "peak_premium": round(state.peak_premium, 4),
        "target_1_banked": state.target_1_banked,
        "elapsed_seconds": round(elapsed_seconds, 1) if elapsed_seconds is not None else None,
    }
