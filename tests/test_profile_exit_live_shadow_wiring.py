"""PART A: profile-exit SHADOW-RECORD dual-run wiring in the live monitor.

These tests pin down the NON-NEGOTIABLE INVARIANT of this wave: with the operator
flag ``profile_exit_drives_live`` OFF (its default, and the only state shipped),
the profile-exit evaluator only RECORDS a shadow decision each tick — it never
drives a real order, cancels/replaces a real stop, or alters what the EXISTING
exit/management path does. Existing exit behavior is byte-for-byte unchanged for
both a profile and a non-profile deployment.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from bhiksha.config.models import DeploymentManifest, ExitSpec
from bhiksha.domain.enums import ExitMode
from bhiksha.execution.order_manager import OrderResult, PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository, SQLiteTradeStateRepository

from test_execution_supervisor import (
    RecordingPlanner,
    StubOrderManager,
    _enabled_deployment,
    _events_of_type,
)
from bhiksha.config.models import AppConfig


BASE_DEPLOYMENT_ID = "market_impulse_qqq_short_v1"
OPTION = "QQQ260401P00556000"


def _profile_exit_spec(base_exit: ExitSpec, *, drives_live: bool = False, shadow_only: bool = True) -> ExitSpec:
    """A v2 operator exit profile pinned onto the base deployment's exit spec.

    Keeps a positive stop (HIGH-2) and disables EOD so the shadow ladder evaluates
    cleanly off the live premium.
    """
    return base_exit.model_copy(
        update={
            "profile_exit_id": "FLASH_REVERSAL",
            "profile_exit_shadow_only": shadow_only,
            "profile_exit_drives_live": drives_live,
            "target_1_r": 1.0,
            "target_2_r": 2.0,
            "target_1_quantity": 1.0,
            "initial_stop_pct": 0.25,
            "premium_disaster_stop_pct": 0.30,
            "high_water_giveback_policy": "OFF",
            "breakeven_after_t1": True,
            "eod_flat": False,
        }
    )


def _profile_deployment(*, drives_live: bool = False):
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = _profile_exit_spec(base.exit, drives_live=drives_live)
    # Re-validate through the model so HIGH-2 / exit-safety validators run.
    return DeploymentManifest(
        deployment_id=base.deployment_id,
        enabled=True,
        symbol=base.symbol,
        strategy=base.strategy,
        execution=base.execution,
        risk=base.risk,
        exit=exit_spec,
        source=base.source,
    )


class _CallRecordingOrderManager(StubOrderManager):
    """Records every broker-affecting call; quote drives the profile ladder."""

    def __init__(self, *, bid: float = 3.0) -> None:
        self.bid = bid
        self.calls: list[tuple] = []

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(symbol=option_symbol, bid=self.bid, ask=self.bid + 0.05, last=self.bid + 0.02, open_interest=500, outcome="SUCCESS")

    async def place_stop_loss_order(self, option_symbol, stop_price, quantity, *, order_id=None):
        self.calls.append(("place_stop", option_symbol, round(stop_price, 2), int(quantity)))
        return OrderResult(order_id="STOP_NEW")

    async def place_target_order(self, option_symbol, limit_price, quantity):
        self.calls.append(("place_target", option_symbol, round(limit_price, 2), int(quantity)))
        return OrderResult(order_id="TARGET_NEW")

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        self.calls.append(("place_close", option_symbol, int(quantity), exit_mode.value))
        return OrderResult(order_id="CLOSE_NEW")

    async def place_square_off_order(self, option_symbol, quantity):
        self.calls.append(("place_square_off", option_symbol, int(quantity)))
        return OrderResult(order_id="CLOSE_NEW")

    async def place_entry_order(self, *args, **kwargs):
        self.calls.append(("place_entry", args, tuple(sorted(kwargs.items()))))
        return OrderResult(order_id="ENTRY_NEW")

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return True, None


def _supervisor(tmp_path, om):
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    return (
        ExecutionSupervisor(
            planner=RecordingPlanner(om),
            event_repository=repo,
            app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        ),
        repo,
    )


def _open_live_position(supervisor, deployment, *, stop_order_id="STOP_OLD", stop_price=2.25, quantity=2, entry=3.0):
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol=OPTION,
        quantity=quantity,
        entry_price=entry,
        entry_timestamp=datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
        source="live_open",
        stop_order_id=stop_order_id,
        stop_price=stop_price,
    )
    return supervisor.planner.position_tracker.active_positions()[0]


# --------------------------------------------------------------------------- #
# The headline INVARIANT: flag OFF => existing exits byte-for-byte unchanged
# --------------------------------------------------------------------------- #


def _breakeven_nonprofile_deployment():
    """Base deployment with breakeven-after-R configured so the EXISTING
    management path takes a REAL action (cancel old stop + place breakeven stop)."""
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = base.exit.model_copy(update={"stop_to_breakeven_after_r_multiple": 0.5})
    return DeploymentManifest(
        deployment_id=base.deployment_id,
        enabled=True,
        symbol=base.symbol,
        strategy=base.strategy,
        execution=base.execution,
        risk=base.risk,
        exit=exit_spec,
        source=base.source,
    )


def _breakeven_profile_deployment(*, drives_live: bool = False):
    """Profile variant that ALSO carries the same existing breakeven-after-R dial,
    so the existing management action and the profile shadow run side by side."""
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = _profile_exit_spec(base.exit, drives_live=drives_live).model_copy(
        update={"stop_to_breakeven_after_r_multiple": 0.5}
    )
    return DeploymentManifest(
        deployment_id=base.deployment_id,
        enabled=True,
        symbol=base.symbol,
        strategy=base.strategy,
        execution=base.execution,
        risk=base.risk,
        exit=exit_spec,
        source=base.source,
    )


def test_invariant_profile_vs_nonprofile_identical_broker_calls_flag_off(tmp_path) -> None:
    """THE NON-NEGOTIABLE INVARIANT. With the flag OFF, a profile deployment makes
    the EXACT same broker calls and ends in the EXACT same tracked-position state
    as the same deployment with NO profile, under identical quotes — even when the
    EXISTING management path takes a real action (breakeven stop promotion). The
    profile branch is record-only.

    entry 3.0, exit.stop_loss_pct 0.45 -> breakeven trigger at
    3.0 * (1 + 0.5*0.45) = 3.675; a bid of 3.8 triggers the existing breakeven
    promotion (cancel STOP_OLD, place a new stop at entry 3.0)."""
    quote_bid = 3.8  # triggers the EXISTING breakeven-after-R promotion

    # --- non-profile baseline (existing breakeven action fires) ---
    om_base = _CallRecordingOrderManager(bid=quote_bid)
    sup_base, _ = _supervisor(tmp_path / "base", om_base)
    dep_base = _breakeven_nonprofile_deployment()
    pos_base = _open_live_position(sup_base, dep_base, stop_order_id="STOP_OLD", stop_price=1.65, entry=3.0)
    managed_base = asyncio.run(sup_base.manage_open_position(dep_base, pos_base, dry_run=False))

    # --- profile variant, flag OFF (default), SAME breakeven dial ---
    om_prof = _CallRecordingOrderManager(bid=quote_bid)
    sup_prof, repo_prof = _supervisor(tmp_path / "prof", om_prof)
    dep_prof = _breakeven_profile_deployment(drives_live=False)
    assert dep_prof.exit.profile_exit_id == "FLASH_REVERSAL"
    assert dep_prof.exit.profile_exit_drives_live is False
    pos_prof = _open_live_position(sup_prof, dep_prof, stop_order_id="STOP_OLD", stop_price=1.65, entry=3.0)
    managed_prof = asyncio.run(sup_prof.manage_open_position(dep_prof, pos_prof, dry_run=False))

    # The existing path DID take a real action (not a trivial empty comparison).
    assert ("cancel", "STOP_OLD") in om_base.calls
    assert any(c[0] == "place_stop" for c in om_base.calls)
    # Existing behavior is byte-for-byte unchanged: identical broker calls...
    assert om_prof.calls == om_base.calls
    # ...and the resulting tracked position is identical.
    assert managed_prof == managed_base

    # The profile DID record a shadow decision (record-only, not a broker call).
    shadow = _events_of_type(repo_prof, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["mode"] == "shadow_record"
    assert shadow[0]["dispatch_allowed"] is False
    # No live dispatch / no routing-ready audit event with the flag OFF.
    assert _events_of_type(repo_prof, "profile_exit_dispatch_ready") == []


def test_invariant_profile_rung_fires_in_shadow_but_no_exit_with_flag_off(tmp_path) -> None:
    """Even when the profile ladder WOULD exit (premium below the profile stop),
    the flag-OFF path records the exit decision but places NO order and leaves the
    position OPEN and unchanged. The existing path is the sole authority."""
    # entry 3.0, profile initial_stop_pct 0.25 -> profile stop at 2.25. A bid of
    # 2.10 is BELOW the profile stop (profile would square_off) but ABOVE the
    # existing resting stop price (2.00) so the existing path does NOT exit.
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=False)
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    # The profile ladder recorded an EXIT decision in shadow...
    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["exit"] is True
    assert shadow[0]["rule"] == "initial_stop"
    assert shadow[0]["dispatch_allowed"] is False
    assert shadow[0]["mode"] == "shadow_record"
    # ...but NO close/square-off order was placed and the position stays open.
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    assert managed is not None
    assert managed.quantity == 2
    assert managed.exit_mode is None and managed.exit_order_id is None
    # No exit_plan / exit_decision was produced by the management path.
    assert _events_of_type(repo, "exit_plan") == []
    # State persisted for the next tick (not yet cleared — position still open).
    assert sup._profile_state_key(managed) in sup._profile_exit_states


def test_shadow_state_persists_across_ticks_and_clears_on_close(tmp_path) -> None:
    """The supervisor-owned ladder state persists tick-to-tick through the live
    management path and is cleared when the position closes."""
    om = _CallRecordingOrderManager(bid=3.0)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=False)
    pos = _open_live_position(sup, dep)
    key = sup._profile_state_key(pos)

    # Two management ticks: each records a shadow decision and reuses the state.
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))
    pos2 = sup.planner.position_tracker.active_positions()[0]
    asyncio.run(sup.manage_open_position(dep, pos2, dry_run=False))

    assert len(_events_of_type(repo, "profile_exit_shadow")) == 2
    assert key in sup._profile_exit_states

    # Terminal close clears the state (NEW-4 lifecycle).
    sup.clear_profile_exit_state(sup.planner.position_tracker.active_positions()[0])
    assert key not in sup._profile_exit_states


def test_nonprofile_deployment_records_no_shadow_event(tmp_path) -> None:
    """A deployment with NO exit profile must never touch the shadow recorder."""
    om = _CallRecordingOrderManager(bid=3.0)
    sup, repo = _supervisor(tmp_path, om)
    dep = _enabled_deployment(BASE_DEPLOYMENT_ID)
    assert dep.exit.profile_exit_id is None
    pos = _open_live_position(sup, dep)
    # Ensure the events table exists even if the benign management tick emits no
    # events (so the "no shadow events" assertion queries a real table).
    asyncio.run(repo.append("test_marker", {"k": "v"}))
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))
    assert _events_of_type(repo, "profile_exit_shadow") == []


def test_flip_seam_when_flag_on_gate_opens_but_route_is_not_enabled(tmp_path) -> None:
    """FLIP SEAM proof. Forcing the operator flag ON (test-only) with a live
    position source opens the fail-closed dispatch gate (``outcome.dispatched``
    True), so the operator's flip is a genuine one-line enablement. THIS WAVE
    still does NOT route: the supervisor emits an audit event and places NO order.

    Bid 2.10 < profile stop 2.25 (entry 3.0, initial_stop_pct 0.25) so the profile
    ladder produces a real square_off; with the existing resting stop at 2.00 the
    EXISTING path does not exit, isolating the seam behavior."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    # drives_live=True (the one-line flip) AND shadow_only=False so the gate's
    # shadow precondition is satisfied; position_source live_open; mode is the
    # canonical live_approval_gated supplied by the wiring.
    dep = _profile_deployment(drives_live=True)
    assert dep.exit.profile_exit_drives_live is True
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    # The gate OPENED: dispatch_allowed True, mode flips to live_dispatch.
    assert shadow[0]["dispatch_allowed"] is True
    assert shadow[0]["mode"] == "live_dispatch"
    assert shadow[0]["exit"] is True
    # The seam emitted its audit marker (the documented place to wire the route).
    ready = _events_of_type(repo, "profile_exit_dispatch_ready")
    assert len(ready) == 1
    assert ready[0]["note"] == "profile_exit_live_routing_not_enabled_this_wave"
    # CRITICAL: even with the gate open, THIS WAVE places NO order and the position
    # is NOT exited by the profile branch (route deliberately not enabled).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    assert managed is not None and managed.exit_mode is None and managed.exit_order_id is None
    assert _events_of_type(repo, "exit_plan") == []


def test_flip_seam_stays_closed_for_nonlive_position_source_even_with_flag_on(tmp_path) -> None:
    """Defense: even with the flag ON, a non-live position source (shadow) keeps
    the fail-closed allowlist CLOSED — the gate needs a live entry source too."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True)
    sup.planner.position_tracker.open_position(
        "QQQ",
        dep.deployment_id,
        trade_id="S1",
        option_symbol=OPTION,
        quantity=2,
        entry_price=3.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
        source="shadow",  # NOT a live entry source
    )
    pos = sup.planner.position_tracker.active_positions()[0]
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))
    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["dispatch_allowed"] is False  # source fails the allowlist
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_shadow_records_for_shadow_source_position_too(tmp_path) -> None:
    """Shadow/dry-run positions also get a recorded profile decision (richer
    audit), and never place an order regardless of the flag."""
    om = _CallRecordingOrderManager(bid=3.0)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=False)
    sup.planner.position_tracker.open_position(
        "QQQ",
        dep.deployment_id,
        trade_id="S1",
        option_symbol=OPTION,
        quantity=2,
        entry_price=3.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
        source="shadow",
    )
    pos = sup.planner.position_tracker.active_positions()[0]
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))
    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["dispatch_allowed"] is False


# --------------------------------------------------------------------------- #
# HIGH-2: profile deployment must have a resolvable recovery stop (config time)
# --------------------------------------------------------------------------- #


def test_high2_profile_deployment_without_recovery_stop_is_rejected() -> None:
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    bad_exit = ExitSpec(
        profile="strategy_managed_v1",
        use_algorithmic_exit=False,  # avoid the unrelated algo-exit safety check
        profile_exit_id="X",
        stop_loss_pct=0.0,
        initial_stop_pct=None,
        premium_disaster_stop_pct=None,
        eod_flat=False,
    )
    bad_risk = base.risk.model_copy(update={"stop_loss_pct": 0.0})
    with pytest.raises(ValueError, match="resolvable recovery stop"):
        DeploymentManifest(
            deployment_id="p_bad",
            symbol="QQQ",
            strategy=base.strategy,
            execution=base.execution,
            risk=bad_risk,
            exit=bad_exit,
        )


def test_high2_profile_deployment_with_exit_stop_is_accepted() -> None:
    dep = _profile_deployment(drives_live=False)  # exit.stop_loss_pct default 0.45
    assert dep.exit.profile_exit_id == "FLASH_REVERSAL"


def test_high2_profile_supplies_own_recovery_floor_when_global_zero() -> None:
    # No exit/risk stop pct, but the profile's initial_stop_pct supplies the floor.
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    prof_exit = ExitSpec(
        profile="strategy_managed_v1",
        use_algorithmic_exit=False,
        profile_exit_id="X",
        stop_loss_pct=0.0,
        initial_stop_pct=0.25,
        eod_flat=False,
    )
    risk0 = base.risk.model_copy(update={"stop_loss_pct": 0.0})
    dep = DeploymentManifest(
        deployment_id="p_floor",
        symbol="QQQ",
        strategy=base.strategy,
        execution=base.execution,
        risk=risk0,
        exit=prof_exit,
    )
    # And the runtime resolver derives the floor from the profile dial.
    from bhiksha.execution.supervisor import _resolved_recovery_stop_loss_pct

    pct, policy = _resolved_recovery_stop_loss_pct(dep)
    assert pct == 0.25
    assert policy == "profile_initial_stop"


def test_high2_resolver_falls_back_to_disaster_stop_for_profile() -> None:
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    prof_exit = ExitSpec(
        profile="strategy_managed_v1",
        use_algorithmic_exit=False,
        profile_exit_id="X",
        stop_loss_pct=0.0,
        initial_stop_pct=None,
        premium_disaster_stop_pct=0.4,
        eod_flat=False,
    )
    risk0 = base.risk.model_copy(update={"stop_loss_pct": 0.0})
    dep = DeploymentManifest(
        deployment_id="p_dis",
        symbol="QQQ",
        strategy=base.strategy,
        execution=base.execution,
        risk=risk0,
        exit=prof_exit,
    )
    from bhiksha.execution.supervisor import _resolved_recovery_stop_loss_pct

    pct, policy = _resolved_recovery_stop_loss_pct(dep)
    assert pct == 0.4
    assert policy == "profile_disaster_stop"
