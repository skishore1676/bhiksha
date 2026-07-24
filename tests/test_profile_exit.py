"""Tests for the profile-aware exit evaluator (shadow-first, premium-anchored).

Covers:
* each priority-ladder rung fires correctly on live-premium inputs;
* the partial-then-breakeven sequence and the runner exit;
* the v2 ManagementPolicySpec / ExitSpec adapters and pre-v2 back-compat;
* the decision -> existing-FSM-action mapping;
* the shadow-vs-live dispatch gate; and
* SHADOW mode records-but-does-not-dispatch (mocked market + event sink).

All market data is mocked — no live API, no broker, no order placement.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dt_time, timezone

import pytest

from bhiksha.config.models import ExitSpec
from bhiksha.domain.models import ExitDecision
from bhiksha.execution.profile_exit import (
    ProfileExitFields,
    ProfileExitState,
    ProfileFsmAction,
    ProfileLadderRule,
    ProfileMarketView,
    evaluate_profile_exit,
    profile_decision_to_exit_decision,
    profile_exit_dispatch_allowed,
)
from bhiksha.execution.profile_exit_shadow import evaluate_and_record_profile_exit
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import ManagementPolicySpec  # noqa: E402

UTC = timezone.utc
ENTRY_TIME = datetime(2026, 6, 13, 14, 0, 0, tzinfo=UTC)


def _flash_reversal_fields(target_1_quantity: float = 0.75) -> ProfileExitFields:
    # FLASH_REVERSAL dials from docs/EXIT_PROFILE_PLAYBOOKS.md (premium terms).
    return ProfileExitFields(
        profile_id="FLASH_REVERSAL",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=target_1_quantity,
        initial_stop_pct=0.25,
        premium_disaster_stop_pct=0.30,
        no_progress_seconds=900,
        max_hold_seconds=5400,
        high_water_giveback_policy="STRICT",
        breakeven_after_t1=True,
        eod_flat=True,
    )


def _market(premium: float | None, *, bar_time: dt_time | None = dt_time(10, 0)) -> ProfileMarketView:
    return ProfileMarketView(current_premium=premium, bar_time_et=bar_time, bid=premium, ask=premium)


def _eval(fields, premium, *, quantity=4, state=None, bar_time=dt_time(10, 0), minutes=1):
    state = state or ProfileExitState.new(1.00)
    return evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=quantity,
        market=_market(premium, bar_time=bar_time),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=minutes),
        state=state,
    ), state


# --------------------------------------------------------------------------- #
# Ladder rung 1: initial stop / disaster stop
# --------------------------------------------------------------------------- #


def test_initial_premium_stop_fires_full_square_off() -> None:
    fields = _flash_reversal_fields()
    # risk = 0.25 * 1.00 = 0.25 -> stop at 0.75; premium 0.74 breaches.
    decision, _ = _eval(fields, 0.74, quantity=4)
    assert decision.rule is ProfileLadderRule.INITIAL_STOP
    assert decision.fsm_action is ProfileFsmAction.SQUARE_OFF
    assert decision.exit is True
    assert decision.exit_quantity == 4
    assert decision.cancel_protection_orders is True
    assert decision.reason == "profile_initial_stop"


def test_premium_disaster_stop_catches_gap_through_after_breakeven() -> None:
    # Realistic operator config: disaster (30%) WIDER than initial (25%). The
    # disaster floor is the absolute catastrophe stop, checked regardless of
    # breakeven state — it catches a gap that blew past the breakeven stop.
    fields = _flash_reversal_fields(target_1_quantity=0.5)
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.26, quantity=4, state=state)  # bank T1, arm breakeven at 1.00
    state.mark_breakeven_emitted()
    # Premium gaps to 0.69 (< disaster price 0.70) — below entry AND below
    # disaster floor. Disaster stop is the rung that fires.
    decision, _ = _eval(fields, 0.69, quantity=4, state=state, minutes=3)
    assert decision.rule is ProfileLadderRule.DISASTER_STOP
    assert decision.reason == "profile_premium_disaster_stop"


def test_initial_stop_takes_precedence_over_wider_disaster() -> None:
    # When the disaster floor is wider than the initial stop, a moderate breach
    # (between the two) fires the INITIAL stop, not disaster.
    fields = _flash_reversal_fields()  # initial 25% -> 0.75, disaster 30% -> 0.70
    decision, _ = _eval(fields, 0.72, quantity=1)  # <= 0.75 but > 0.70
    assert decision.rule is ProfileLadderRule.INITIAL_STOP


def test_stop_does_not_fire_above_stop_price() -> None:
    fields = _flash_reversal_fields()
    decision, _ = _eval(fields, 1.05, quantity=4)  # in-the-money but no rung
    assert decision.rule is ProfileLadderRule.HOLD
    assert decision.fsm_action is ProfileFsmAction.HOLD
    assert decision.exit is False


# --------------------------------------------------------------------------- #
# Ladder rung 2: target-1 partial + breakeven ratchet
# --------------------------------------------------------------------------- #


def test_target_1_partial_banks_fraction_and_arms_breakeven() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.75)
    # T1 at 1.25; premium 1.26 -> bank 75% of 4 = 3 contracts.
    decision, state = _eval(fields, 1.26, quantity=4)
    assert decision.rule is ProfileLadderRule.TARGET_1_PARTIAL
    assert decision.fsm_action is ProfileFsmAction.PARTIAL_SCALE
    assert decision.is_partial is True
    assert decision.exit is True
    assert decision.exit_quantity == 3
    assert decision.features["remaining_quantity"] == 1
    assert decision.features["arms_breakeven"] is True
    assert state.target_1_banked is True
    assert state.stop_at_breakeven is True


def test_breakeven_stop_to_breakeven_emitted_on_next_tick() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.75)
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.26, quantity=4, state=state)  # bank T1
    # Next tick, premium still up: should emit STOP_TO_BREAKEVEN (a hold-class FSM
    # action that tightens the live stop, NOT an exit).
    decision, _ = _eval(fields, 1.22, quantity=4, state=state, minutes=2)
    assert decision.rule is ProfileLadderRule.TARGET_1_PARTIAL
    assert decision.fsm_action is ProfileFsmAction.STOP_TO_BREAKEVEN
    assert decision.exit is False
    assert decision.target_stop_price == 1.00
    # Emitted only once.
    decision3, _ = _eval(fields, 1.21, quantity=4, state=state, minutes=3)
    assert decision3.fsm_action is not ProfileFsmAction.STOP_TO_BREAKEVEN


def test_breakeven_stop_fires_after_t1_when_premium_returns_to_entry() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.75)
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.26, quantity=4, state=state)  # bank T1, arm breakeven
    state.mark_breakeven_emitted()  # skip the breakeven-emit tick
    # Premium falls back to 0.99 (<= entry) -> breakeven stop on the runner (1 qty).
    decision, _ = _eval(fields, 0.99, quantity=4, state=state, minutes=3)
    assert decision.rule is ProfileLadderRule.INITIAL_STOP
    assert decision.reason == "profile_breakeven_stop"
    assert decision.exit_quantity == 1  # only the runner remains


def test_target_1_full_when_quantity_is_one() -> None:
    fields = _flash_reversal_fields(target_1_quantity=1.0)  # no partial
    decision, _ = _eval(fields, 1.30, quantity=4)
    assert decision.rule is ProfileLadderRule.TARGET_1_PARTIAL
    assert decision.fsm_action is ProfileFsmAction.SQUARE_OFF  # full exit, not partial
    assert decision.exit_quantity == 4
    assert decision.reason == "profile_target_1_full"


# --------------------------------------------------------------------------- #
# Ladder rung 3: target-2 runner
# --------------------------------------------------------------------------- #


def test_target_2_runner_exit_after_partial() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.5)
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.26, quantity=2, state=state)  # bank T1 -> 1 contract banked
    state.mark_breakeven_emitted()
    # T2 at 1.50; premium 1.51 -> exit the runner.
    decision, _ = _eval(fields, 1.51, quantity=2, state=state, minutes=2)
    assert decision.rule is ProfileLadderRule.TARGET_2_RUNNER
    assert decision.fsm_action is ProfileFsmAction.SQUARE_OFF
    assert decision.exit_quantity == 1


def test_single_target_runner_when_no_partial_configured() -> None:
    # No target_1 -> target_2 acts as the lone target on the full position.
    fields = ProfileExitFields(profile_id="S", initial_stop_pct=0.25, target_1_r=None, target_2_r=2.0)
    decision, _ = _eval(fields, 1.51, quantity=3)
    assert decision.rule is ProfileLadderRule.TARGET_2_RUNNER
    assert decision.exit_quantity == 3


# --------------------------------------------------------------------------- #
# Ladder rung 4: high-water giveback
# --------------------------------------------------------------------------- #


def test_high_water_giveback_arms_then_fires() -> None:
    fields = ProfileExitFields(
        profile_id="G",
        initial_stop_pct=0.25,
        target_1_r=None,
        target_2_r=10.0,  # far away so giveback can fire first
        high_water_giveback_policy="STRICT",  # arm 1.0R, retrace 0.33
    )
    state = ProfileExitState.new(1.00)
    # Peak premium 1.30 (r = 1.2) sets the high-water mark.
    _eval(fields, 1.30, quantity=1, state=state)
    # Floor = 1.2 * (1 - 0.33) = 0.804R -> price ~1.201; premium 1.15 (r=0.6) gives back.
    decision, _ = _eval(fields, 1.15, quantity=1, state=state, minutes=2)
    assert decision.rule is ProfileLadderRule.HIGH_WATER_GIVEBACK
    assert decision.fsm_action is ProfileFsmAction.SQUARE_OFF
    assert decision.reason == "profile_high_water_giveback:STRICT"
    assert decision.features["peak_r"] == pytest.approx(1.2, abs=1e-6)


def test_giveback_off_never_fires() -> None:
    fields = ProfileExitFields(
        profile_id="G",
        initial_stop_pct=0.25,
        target_1_r=None,
        target_2_r=10.0,
        high_water_giveback_policy="OFF",
    )
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.30, quantity=1, state=state)
    decision, _ = _eval(fields, 1.15, quantity=1, state=state, minutes=2)
    assert decision.rule is ProfileLadderRule.HOLD


# --------------------------------------------------------------------------- #
# Ladder rung 5: time stops (max hold, no progress)
# --------------------------------------------------------------------------- #


def test_max_hold_time_stop_fires() -> None:
    fields = ProfileExitFields(profile_id="M", initial_stop_pct=0.25, max_hold_seconds=300, target_2_r=10.0)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=_market(1.05),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=6),  # 360s elapsed > 300s
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.MAX_HOLD
    assert decision.reason == "profile_max_hold:300s"


def test_no_progress_time_stop_fires_when_flat() -> None:
    fields = ProfileExitFields(profile_id="N", initial_stop_pct=0.25, no_progress_seconds=300, target_2_r=10.0)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=_market(1.02),  # peak_r ~ 0.08 < 0.25 floor
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=6),
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.NO_PROGRESS
    assert decision.reason == "profile_no_progress:300s"


def test_no_progress_does_not_fire_when_thesis_made_progress() -> None:
    fields = ProfileExitFields(profile_id="N", initial_stop_pct=0.25, no_progress_seconds=300, target_2_r=10.0)
    state = ProfileExitState.new(1.00)
    # Prior tick pushed peak to 1.20 (r ~ 0.8 > 0.25), so no-progress must not fire.
    _eval(fields, 1.20, quantity=1, state=state, minutes=1)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=_market(1.05),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=6),
        state=state,
    )
    assert decision.rule is ProfileLadderRule.HOLD


def test_july_20_qqq_path_holds_at_point_eight_r_then_exits_at_original_stop() -> None:
    """Golden fixture for the pre-envelope behavior that motivated Exit Engine V2.

    The verified trade entered at 2.69, peaked at 3.44 (+0.7966R on a 35%
    premium risk budget), never reached T1 or the current MODERATE 1.25R
    giveback arm, and ultimately exited at the original 1.75 stop. Increment 1
    may record envelope counterfactuals for this path but must not change these
    live ladder decisions.
    """

    entry = 2.69
    fields = ProfileExitFields(
        profile_id="TREND_CONTINUATION",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.60,
        initial_stop_pct=0.35,
        no_progress_seconds=900,
        high_water_giveback_policy="MODERATE",
        eod_flat=False,
    )
    state = ProfileExitState.new(entry, seed_quantity=5)

    peak = evaluate_profile_exit(
        fields=fields,
        entry_premium=entry,
        quantity=5,
        market=_market(3.44),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=1),
        state=state,
    )
    assert peak.rule is ProfileLadderRule.HOLD
    assert state.peak_premium == pytest.approx(3.44)

    worked_then_retraced = evaluate_profile_exit(
        fields=fields,
        entry_premium=entry,
        quantity=5,
        market=_market(2.34),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=20),
        state=state,
    )
    assert worked_then_retraced.rule is ProfileLadderRule.HOLD

    broker_stop_fill_tick = evaluate_profile_exit(
        fields=fields,
        entry_premium=entry,
        quantity=5,
        market=_market(1.75),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=21),
        state=state,
    )
    # The profile evaluator's exact floor is 2.69 * (1 - 0.35) = 1.7485, so
    # the rounded $1.75 broker stop can fill while the profile path still says
    # HOLD. This distinction is intentional: the broker protection, not a
    # profile dispatch, closed the verified trade.
    assert broker_stop_fill_tick.rule is ProfileLadderRule.HOLD
    assert broker_stop_fill_tick.features["current_r"] == pytest.approx(-0.9984)


# --------------------------------------------------------------------------- #
# Ladder rung 6: EOD flat (highest precedence when enabled)
# --------------------------------------------------------------------------- #


def test_eod_flat_fires_hard_flat_action() -> None:
    fields = ProfileExitFields(profile_id="E", initial_stop_pct=0.25, eod_flat=True, hard_flat_time_et="15:55")
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=2,
        market=_market(1.10, bar_time=dt_time(15, 56)),
        entry_time=ENTRY_TIME,
        now=datetime(2026, 6, 13, 19, 56, tzinfo=UTC),
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.EOD_FLAT
    assert decision.fsm_action is ProfileFsmAction.HARD_FLAT
    assert decision.exit is True
    assert decision.exit_quantity == 2


def test_eod_flat_off_lets_position_ride_past_close() -> None:
    fields = ProfileExitFields(profile_id="E", initial_stop_pct=0.25, eod_flat=False, target_2_r=10.0)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=_market(1.10, bar_time=dt_time(15, 56)),
        entry_time=ENTRY_TIME,
        now=datetime(2026, 6, 13, 19, 56, tzinfo=UTC),
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.HOLD


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_missing_premium_only_time_rules_can_fire() -> None:
    fields = _flash_reversal_fields()
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=ProfileMarketView(current_premium=None, bar_time_et=dt_time(10, 0)),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=1),
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.HOLD
    assert decision.reason == "profile_premium_unavailable"


def test_zero_entry_premium_holds() -> None:
    fields = _flash_reversal_fields()
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=0.0,
        quantity=1,
        market=_market(0.5),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=1),
        state=ProfileExitState.new(0.0),
    )
    assert decision.rule is ProfileLadderRule.HOLD


# --------------------------------------------------------------------------- #
# M1: EOD robustness — must not silently skip when bar_time_et is absent
# --------------------------------------------------------------------------- #


def test_eod_required_but_missing_bar_time_raises() -> None:
    # eod_flat enabled + require_bar_time_for_eod + no bar clock -> fail loud,
    # never silently let the position ride past hard-flat.
    fields = ProfileExitFields(profile_id="E", initial_stop_pct=0.25, eod_flat=True)
    with pytest.raises(ValueError, match="eod_flat is enabled but no bar_time_et"):
        evaluate_profile_exit(
            fields=fields,
            entry_premium=1.00,
            quantity=1,
            market=ProfileMarketView(current_premium=1.05, bar_time_et=None),
            entry_time=ENTRY_TIME,
            now=ENTRY_TIME.replace(minute=1),
            state=ProfileExitState.new(1.00),
            require_bar_time_for_eod=True,
        )


def test_eod_with_bar_time_supplied_fires_even_when_required() -> None:
    # When the supervisor supplies the bar clock (its _bar_time_et helper), EOD
    # fires normally under the strict flag.
    fields = ProfileExitFields(profile_id="E", initial_stop_pct=0.25, eod_flat=True, hard_flat_time_et="15:55")
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=2,
        market=_market(1.10, bar_time=dt_time(15, 56)),
        entry_time=ENTRY_TIME,
        now=datetime(2026, 6, 13, 19, 56, tzinfo=UTC),
        state=ProfileExitState.new(1.00),
        require_bar_time_for_eod=True,
    )
    assert decision.rule is ProfileLadderRule.EOD_FLAT


def test_eod_missing_bar_time_does_not_raise_when_not_required() -> None:
    # Back-compat: pure-unit callers (require flag default False) still skip EOD
    # gracefully when no bar clock is present.
    fields = ProfileExitFields(profile_id="E", initial_stop_pct=0.25, eod_flat=True, target_2_r=10.0)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=ProfileMarketView(current_premium=1.05, bar_time_et=None),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=1),
        state=ProfileExitState.new(1.00),
    )
    assert decision.rule is ProfileLadderRule.HOLD


# --------------------------------------------------------------------------- #
# M2: reject inverted stop config (disaster tighter than initial)
# --------------------------------------------------------------------------- #


def test_inverted_stop_config_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="tighter than initial_stop_pct"):
        ProfileExitFields(
            profile_id="BAD",
            initial_stop_pct=0.30,
            premium_disaster_stop_pct=0.20,  # tighter than initial -> invalid
        )


def test_disaster_equal_or_wider_than_initial_is_accepted() -> None:
    # Equal is allowed (>=). Wider is the normal operator config.
    ProfileExitFields(profile_id="OK1", initial_stop_pct=0.25, premium_disaster_stop_pct=0.25)
    ProfileExitFields(profile_id="OK2", initial_stop_pct=0.25, premium_disaster_stop_pct=0.30)


def test_exit_spec_rejects_inverted_stop_config() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must not be tighter than initial_stop_pct"):
        ExitSpec(initial_stop_pct=0.30, premium_disaster_stop_pct=0.20)


# --------------------------------------------------------------------------- #
# L1: configurable no-progress favorable floor (default 0.25)
# --------------------------------------------------------------------------- #


def test_no_progress_floor_defaults_to_quarter_r() -> None:
    fields = ProfileExitFields(profile_id="N", initial_stop_pct=0.25, no_progress_seconds=300, target_2_r=10.0)
    assert fields.no_progress_favorable_floor_r == 0.25


def test_no_progress_floor_is_configurable_and_used() -> None:
    # With a HIGHER floor (0.9R), a peak that previously cleared the 0.25 default
    # now counts as "no progress" and the time stop fires.
    fields = ProfileExitFields(
        profile_id="N",
        initial_stop_pct=0.25,
        no_progress_seconds=300,
        target_2_r=10.0,
        no_progress_favorable_floor_r=0.9,
    )
    state = ProfileExitState.new(1.00)
    # Prior tick peaks at 1.10 -> peak_r = 0.4 (< 0.9 floor) so no-progress fires.
    _eval(fields, 1.10, quantity=1, state=state, minutes=1)
    decision = evaluate_profile_exit(
        fields=fields,
        entry_premium=1.00,
        quantity=1,
        market=_market(1.05),
        entry_time=ENTRY_TIME,
        now=ENTRY_TIME.replace(minute=6),
        state=state,
    )
    assert decision.rule is ProfileLadderRule.NO_PROGRESS


def test_exit_spec_no_progress_floor_flows_into_fields() -> None:
    spec = ExitSpec(profile_exit_id="P", initial_stop_pct=0.25, no_progress_favorable_floor_r=0.5)
    fields = ProfileExitFields.from_exit_spec(spec)
    assert fields.no_progress_favorable_floor_r == 0.5


# --------------------------------------------------------------------------- #
# Adapters: kernel ManagementPolicySpec v2 + Bhiksha ExitSpec + back-compat
# --------------------------------------------------------------------------- #


def test_from_management_spec_reads_all_v2_fields() -> None:
    spec = ManagementPolicySpec(
        policy_id="FLASH_REVERSAL",
        stop_family="premium_pct",
        stop_anchor="option_premium",
        exit_family="r_multiple",
        target_model="staged",
        target_r=2.0,
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.75,
        initial_stop_pct=0.25,
        premium_disaster_stop_pct=0.30,
        no_progress_seconds=900,
        max_hold_seconds=5400,
        high_water_giveback_policy="STRICT",
        breakeven_after_t1=True,
        eod_flat=True,
    )
    fields = ProfileExitFields.from_management_spec(spec)
    assert fields.profile_id == "FLASH_REVERSAL"
    assert fields.stop_pct == 0.25
    assert fields.effective_target_1_r == 1.0
    assert fields.effective_target_2_r == 2.0
    assert fields.giveback_arm_r == 1.0
    assert fields.disaster_stop_pct == 0.30
    assert fields.max_hold_seconds == 5400


def test_from_management_spec_dict_path() -> None:
    spec = ManagementPolicySpec(
        policy_id="X",
        stop_family="premium_pct",
        stop_anchor="option_premium",
        exit_family="r_multiple",
        target_model="single",
        target_r=2.0,
        initial_stop_pct=0.25,
    )
    fields = ProfileExitFields.from_management_spec(spec.model_dump())
    assert fields.profile_id == "X"
    assert fields.eod_flat is True


def test_pre_v2_spec_is_back_compatible() -> None:
    # A spec with only the original fields -> single target, no partial, no giveback.
    spec = ManagementPolicySpec(
        policy_id="LEGACY",
        stop_family="premium_pct",
        stop_anchor="option_premium",
        exit_family="r_multiple",
        target_model="single",
        target_r=2.0,
    )
    fields = ProfileExitFields.from_management_spec(spec)
    assert fields.effective_target_1_r is None
    assert fields.effective_target_2_r == 2.0
    assert fields.target_1_quantity == 1.0
    assert fields.giveback_arm_r is None
    # And it evaluates as a single full-exit target on the whole position.
    decision, _ = _eval(fields, 1.91, quantity=2)  # target_r=2.0 -> 1.0 + 2*0.45=1.90
    assert decision.rule is ProfileLadderRule.TARGET_2_RUNNER
    assert decision.exit_quantity == 2


def test_from_exit_spec_reads_v2_dials() -> None:
    spec = ExitSpec(
        profile_exit_id="TREND_CONTINUATION",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.6,
        initial_stop_pct=0.30,
        premium_disaster_stop_pct=0.35,
        no_progress_seconds=2700,
        max_hold_seconds=10800,
        high_water_giveback_policy="MODERATE",
        stop_loss_pct=0.30,
    )
    fields = ProfileExitFields.from_exit_spec(spec)
    assert fields.profile_id == "TREND_CONTINUATION"
    assert fields.target_1_quantity == 0.6
    assert fields.giveback_arm_r == 1.25
    assert fields.max_hold_seconds == 10800


def test_explicit_zero_target_1_quantity_is_preserved_by_adapters() -> None:
    # target_1_quantity == 0 is a VALID operator profile (kernel ge=0): bank
    # nothing at T1, let the whole position ride to T2. A ``... or 1.0`` fallback
    # would silently rewrite the falsy 0 to the 1.0 default, which means "exit the
    # entire position at T1" — the exact opposite intent. All three adapters must
    # keep an explicit 0.
    spec = ExitSpec(
        profile_exit_id="RIDE_TO_T2",
        target_1_r=1.0,
        target_2_r=3.0,
        target_1_quantity=0.0,
        initial_stop_pct=0.40,
        stop_loss_pct=0.40,
    )
    assert ProfileExitFields.from_exit_spec(spec).target_1_quantity == 0.0

    mgmt = ManagementPolicySpec(
        policy_id="RIDE_TO_T2",
        stop_family="premium_pct",
        stop_anchor="option_premium",
        exit_family="r_multiple",
        target_model="staged",
        target_r=3.0,
        target_1_r=1.0,
        target_2_r=3.0,
        target_1_quantity=0.0,
        initial_stop_pct=0.40,
    )
    assert ProfileExitFields.from_management_spec(mgmt).target_1_quantity == 0.0
    assert ProfileExitFields.from_management_spec(mgmt.model_dump()).target_1_quantity == 0.0
    # The dict adapter also accepts serialized values, so preserve a valid
    # string zero rather than treating it as a missing quantity.
    mgmt_dict = mgmt.model_dump()
    mgmt_dict["target_1_quantity"] = "0"
    assert ProfileExitFields.from_management_spec(mgmt_dict).target_1_quantity == 0.0
    # Invalid values must retain the prior fail-loud behavior instead of being
    # silently converted into a full T1 exit (the 1.0 default).
    mgmt_dict["target_1_quantity"] = "invalid"
    with pytest.raises(ValueError):
        ProfileExitFields.from_management_spec(mgmt_dict)

    params = {
        "target_1_r": 1.0,
        "target_2_r": 3.0,
        "target_1_quantity": 0.0,
        "initial_stop_pct": 0.40,
    }
    assert ProfileExitFields.from_exit_params("RIDE_TO_T2", params).target_1_quantity == 0.0

    # A MISSING quantity still defaults to the full-position 1.0 (back-compat).
    assert ProfileExitFields.from_exit_params("X", {}).target_1_quantity == 1.0


def test_zero_target_1_quantity_holds_through_t1_instead_of_exiting() -> None:
    # End-to-end ladder proof: with target_1_quantity == 0 a touch of the T1 level
    # must NOT fire any exit (the position rides to T2). Before the fix the
    # coerced-to-1.0 quantity produced a FULL square_off of the whole position at
    # T1 — an inverted decision.
    fields = ProfileExitFields.from_exit_spec(
        ExitSpec(
            profile_exit_id="RIDE_TO_T2",
            target_1_r=1.0,
            target_2_r=3.0,
            target_1_quantity=0.0,
            initial_stop_pct=0.40,
            stop_loss_pct=0.40,
        )
    )
    # entry 1.00, risk = 0.40, T1 price = 1.00 + 1.0 * 0.40 = 1.40.
    decision, state = _eval(fields, 1.40, quantity=10)
    assert decision.exit is False
    assert decision.rule is ProfileLadderRule.HOLD
    assert state.target_1_banked is False

    # A genuine T2 touch (1.00 + 3.0 * 0.40 = 2.20) still exits the full position.
    decision2, _ = _eval(fields, 2.20, quantity=10)
    assert decision2.exit is True
    assert decision2.rule is ProfileLadderRule.TARGET_2_RUNNER
    assert decision2.exit_quantity == 10


# --------------------------------------------------------------------------- #
# Decision -> existing FSM-action / ExitDecision mapping
# --------------------------------------------------------------------------- #


def test_square_off_maps_to_exit_decision_square_off() -> None:
    fields = _flash_reversal_fields()
    decision, _ = _eval(fields, 0.74, quantity=4)  # initial stop
    ed = profile_decision_to_exit_decision(decision, deployment_id="dep", symbol="IWM", timestamp=ENTRY_TIME)
    assert isinstance(ed, ExitDecision)
    assert ed.action == "square_off"
    assert ed.exit is True
    assert ed.cancel_protection_orders is True
    assert ed.reason == ["profile_initial_stop"]
    assert ed.features["profile_fsm_action"] == "square_off"


def test_partial_maps_to_square_off_with_exit_quantity() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.75)
    decision, _ = _eval(fields, 1.26, quantity=4)
    ed = profile_decision_to_exit_decision(decision, deployment_id="dep", symbol="IWM", timestamp=ENTRY_TIME)
    assert ed.action == "square_off"  # existing action; partial conveyed via features
    assert ed.exit is True
    assert ed.features["exit_quantity"] == 3
    assert ed.features["partial_scale"] is True


def test_stop_to_breakeven_maps_to_hold_with_replacement_stop() -> None:
    fields = _flash_reversal_fields(target_1_quantity=0.75)
    state = ProfileExitState.new(1.00)
    _eval(fields, 1.26, quantity=4, state=state)
    decision, _ = _eval(fields, 1.22, quantity=4, state=state, minutes=2)  # STOP_TO_BREAKEVEN
    ed = profile_decision_to_exit_decision(decision, deployment_id="dep", symbol="IWM", timestamp=ENTRY_TIME)
    # No exit order: the supervisor's existing breakeven-promotion branch applies
    # the stop tighten. We surface the new stop via replacement_stop_price.
    assert ed.action == "hold"
    assert ed.exit is False
    assert ed.replacement_stop_price == 1.00


# --------------------------------------------------------------------------- #
# Shadow-vs-live gate
# --------------------------------------------------------------------------- #


def test_dispatch_gate_closed_outside_live() -> None:
    assert profile_exit_dispatch_allowed(live=False, deployment_shadow_only=False, position_source="runtime") is False


def test_dispatch_gate_closed_when_shadow_only() -> None:
    assert profile_exit_dispatch_allowed(live=True, deployment_shadow_only=True, position_source="runtime") is False


def test_dispatch_gate_closed_for_shadow_position() -> None:
    assert profile_exit_dispatch_allowed(live=True, deployment_shadow_only=False, position_source="shadow") is False
    assert profile_exit_dispatch_allowed(live=True, deployment_shadow_only=False, position_source="dry_run") is False


def test_dispatch_gate_closed_for_advisory_runtime_mode() -> None:
    assert (
        profile_exit_dispatch_allowed(
            live=True, deployment_shadow_only=False, position_source="runtime", runtime_mode="shadow"
        )
        is False
    )


def test_dispatch_gate_open_only_when_all_live_preconditions_hold() -> None:
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode="live_approval_gated",
        )
        is True
    )
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_pending",
            runtime_mode="live_approval_gated",
        )
        is True
    )


def test_dispatch_gate_normalizes_runtime_mode_enum() -> None:
    # NEW-5: a kernel-style RuntimeMode ENUM (not the plain string) whose .value is
    # "live_approval_gated" must be normalized so a correctly-configured live
    # deployment can dispatch, instead of silently failing closed on enum-vs-str.
    from enum import Enum

    class RuntimeMode(str, Enum):
        LIVE_APPROVAL_GATED = "live_approval_gated"
        LIVE_AUTOMATED = "live_automated"
        SHADOW = "shadow"

    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
        )
        is True
    )
    # A non-str Enum carrying the same .value also normalizes (defensive).
    class PlainRuntimeMode(Enum):
        LIVE_APPROVAL_GATED = "live_approval_gated"

    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode=PlainRuntimeMode.LIVE_APPROVAL_GATED,
        )
        is True
    )
    # The forbidden enum still fails closed after normalization.
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode=RuntimeMode.LIVE_AUTOMATED,
        )
        is False
    )


# --- H1: strict fail-closed allowlist (C2 forbids live_automated) ---


def test_dispatch_gate_forbids_live_automated_runtime_mode() -> None:
    # C2: live_automated is NOT an allowed dispatch mode (matches every other
    # bhiksha gate). Only live_approval_gated may dispatch.
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode="live_automated",
        )
        is False
    )


def test_dispatch_gate_fails_closed_when_runtime_mode_missing() -> None:
    # H1: a None/unset runtime mode must NOT open the gate (no "gate elsewhere"
    # escape hatch).
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode=None,
        )
        is False
    )


def test_dispatch_gate_fails_closed_for_unknown_or_typo_runtime_mode() -> None:
    for mode in ("LIVE_APPROVAL_GATED", "approval_gated", "advisory", "live", ""):
        assert (
            profile_exit_dispatch_allowed(
                live=True,
                deployment_shadow_only=False,
                position_source="live_open",
                runtime_mode=mode,
            )
            is False
        ), mode


def test_dispatch_gate_fails_closed_for_none_position_source() -> None:
    # H1: None / unknown / non-live-entry sources must all fail closed.
    assert (
        profile_exit_dispatch_allowed(
            live=True,
            deployment_shadow_only=False,
            position_source=None,
            runtime_mode="live_approval_gated",
        )
        is False
    )


def test_dispatch_gate_fails_closed_for_each_non_live_position_source() -> None:
    # Broker-recovered/reconcile sources (true entry premium/ladder state not
    # originated by us), placeholders, typos -> never dispatch.
    for source in (
        "runtime",
        "broker_sync",
        "broker_recovered",
        "packet_runtime_controls",
        "live_OPEN",  # typo / wrong case
        "unknown",
        "",
    ):
        assert (
            profile_exit_dispatch_allowed(
                live=True,
                deployment_shadow_only=False,
                position_source=source,
                runtime_mode="live_approval_gated",
            )
            is False
        ), source


def test_dispatch_gate_allows_only_explicit_live_entry_sources() -> None:
    for source in ("live_open", "live_pending"):
        assert (
            profile_exit_dispatch_allowed(
                live=True,
                deployment_shadow_only=False,
                position_source=source,
                runtime_mode="live_approval_gated",
            )
            is True
        ), source


# --------------------------------------------------------------------------- #
# Shadow recorder: records-but-does-not-dispatch
# --------------------------------------------------------------------------- #


class _RecordingSink:
    """Mock event repository capturing append() calls; no broker side effects."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def append(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def test_shadow_mode_records_exit_but_does_not_dispatch() -> None:
    sink = _RecordingSink()
    fields = _flash_reversal_fields()
    outcome = asyncio.run(
        evaluate_and_record_profile_exit(
            event_sink=sink,
            fields=fields,
            deployment_id="dep",
            symbol="IWM",
            option_symbol="IWM260620C00200000",
            entry_premium=1.00,
            quantity=4,
            market=_market(0.74),  # initial stop would fire
            entry_time=ENTRY_TIME,
            state=ProfileExitState.new(1.00),
            live=False,  # SHADOW
            deployment_shadow_only=False,
            position_source="runtime",
            now=ENTRY_TIME.replace(minute=1),
        )
    )
    # An exit was computed...
    assert outcome.decision.rule is ProfileLadderRule.INITIAL_STOP
    assert outcome.decision.exit is True
    # ...but it was recorded only, NOT dispatched.
    assert outcome.recorded is True
    assert outcome.dispatched is False
    assert outcome.exit_decision is None
    assert len(sink.events) == 1
    event_type, payload = sink.events[0]
    assert event_type == "profile_exit_shadow"
    assert payload["mode"] == "shadow_record"
    assert payload["dispatch_allowed"] is False
    assert payload["exit"] is True
    assert payload["rule"] == "initial_stop"


def test_shadow_mode_records_even_a_hold() -> None:
    sink = _RecordingSink()
    fields = _flash_reversal_fields()
    outcome = asyncio.run(
        evaluate_and_record_profile_exit(
            event_sink=sink,
            fields=fields,
            deployment_id="dep",
            symbol="IWM",
            option_symbol="IWM260620C00200000",
            entry_premium=1.00,
            quantity=4,
            market=_market(1.05),  # no rung -> hold
            entry_time=ENTRY_TIME,
            state=ProfileExitState.new(1.00),
            live=False,
            deployment_shadow_only=False,
            position_source="runtime",
            now=ENTRY_TIME.replace(minute=1),
        )
    )
    assert outcome.decision.rule is ProfileLadderRule.HOLD
    assert outcome.dispatched is False
    assert len(sink.events) == 1  # holds are recorded for auditability


def test_live_gate_open_returns_exit_decision_for_dispatch() -> None:
    # Live preconditions all satisfied -> recorder hands back a mapped ExitDecision
    # for the EXISTING dispatch path (it still does not place orders itself).
    sink = _RecordingSink()
    fields = _flash_reversal_fields()
    outcome = asyncio.run(
        evaluate_and_record_profile_exit(
            event_sink=sink,
            fields=fields,
            deployment_id="dep",
            symbol="IWM",
            option_symbol="IWM260620C00200000",
            entry_premium=1.00,
            quantity=4,
            market=_market(0.74),
            entry_time=ENTRY_TIME,
            state=ProfileExitState.new(1.00),
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",
            runtime_mode="live_approval_gated",
            now=ENTRY_TIME.replace(minute=1),
        )
    )
    assert outcome.dispatched is True
    assert isinstance(outcome.exit_decision, ExitDecision)
    assert outcome.exit_decision.action == "square_off"
    event_type, payload = sink.events[0]
    assert payload["mode"] == "live_dispatch"
    assert payload["dispatch_allowed"] is True


def test_shadow_recorder_never_dispatches_a_hold_even_when_live() -> None:
    sink = _RecordingSink()
    fields = _flash_reversal_fields()
    outcome = asyncio.run(
        evaluate_and_record_profile_exit(
            event_sink=sink,
            fields=fields,
            deployment_id="dep",
            symbol="IWM",
            option_symbol="IWM260620C00200000",
            entry_premium=1.00,
            quantity=4,
            market=_market(1.05),  # hold
            entry_time=ENTRY_TIME,
            state=ProfileExitState.new(1.00),
            live=True,
            deployment_shadow_only=False,
            position_source="live_open",  # gate is open; the HOLD is what blocks dispatch
            runtime_mode="live_approval_gated",
            now=ENTRY_TIME.replace(minute=1),
        )
    )
    assert outcome.decision.rule is ProfileLadderRule.HOLD
    assert outcome.dispatched is False  # nothing to dispatch on a hold
    assert outcome.exit_decision is None


def test_profile_state_identity_mismatch_reseeds_ladder() -> None:
    """Audit backstop (2026-07-02): a cached ladder seeded by a DIFFERENT fill
    (entry premium >10% off, or banked quantity exceeding the ORIGINAL seeded
    quantity) must reseed instead of driving exits off the other fill's state."""
    from bhiksha.execution.supervisor import _profile_state_identity_mismatch

    state = ProfileExitState.new(8.8, seed_quantity=3)
    state.target_1_banked = True
    state.banked_quantity = 2
    state.peak_premium = 24.4

    # Same fill, small jitter: no mismatch.
    assert _profile_state_identity_mismatch(state, entry_premium=8.85) is False
    # Different fill (premium far off): mismatch.
    assert _profile_state_identity_mismatch(state, entry_premium=24.4) is True
    # Banked more than the ORIGINAL seed quantity: impossible for the same fill.
    state.banked_quantity = 4
    assert _profile_state_identity_mismatch(state, entry_premium=8.8) is True
    # Legacy state without seeds (pre-field): both checks skipped, no reseed.
    state.seed_entry_premium = None
    state.seed_quantity = None
    assert _profile_state_identity_mismatch(state, entry_premium=24.4) is False


def test_post_partial_residual_tick_does_not_reseed() -> None:
    """RE-AUDIT BLOCKER (2026-07-02): after a routine T1 partial the tracked
    position holds only the RESIDUAL quantity, so banked > residual is the
    NORMAL state — the identity backstop must NOT reseed (the earlier
    residual-based check refired T1 and closed the T2 runner)."""
    from bhiksha.execution.supervisor import _profile_state_identity_mismatch

    # qty=3 fill at 8.8; T1 banks 60% => banked=2, residual position qty=1.
    state = ProfileExitState.new(8.8, seed_quantity=3)
    state.target_1_banked = True
    state.banked_quantity = 2
    state.stop_at_breakeven = True

    assert _profile_state_identity_mismatch(state, entry_premium=8.8) is False

    # qty=5, bank 60% => banked=3, residual=2: also must not reseed.
    state5 = ProfileExitState.new(4.0, seed_quantity=5)
    state5.target_1_banked = True
    state5.banked_quantity = 3
    assert _profile_state_identity_mismatch(state5, entry_premium=4.0) is False
