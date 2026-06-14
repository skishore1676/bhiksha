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


def _profile_deployment(*, drives_live: bool = False, runtime_mode: str | None = None):
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = _profile_exit_spec(base.exit, drives_live=drives_live)
    # HIGH-1: the gate now reads the deployment's REAL runtime mode from
    # ``execution.runtime_mode`` (no hardcoded literal). Default ``None`` fails
    # closed; a test opts into a genuine live runtime by passing ``runtime_mode``.
    execution = base.execution.model_copy(update={"runtime_mode": runtime_mode})
    # Re-validate through the model so HIGH-2 / exit-safety validators run.
    return DeploymentManifest(
        deployment_id=base.deployment_id,
        enabled=True,
        symbol=base.symbol,
        strategy=base.strategy,
        execution=execution,
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
    # No live dispatch with the flag OFF: the armed route never fired.
    assert _events_of_type(repo_prof, "profile_exit_dispatch_routed") == []


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


def test_armed_route_dispatches_full_close_through_existing_handler_in_dry_run(tmp_path) -> None:
    """ARMED ROUTE (Phase 2). Forcing the operator flag ON (test-only) with a live
    position source AND a live_approval_gated runtime mode opens the fail-closed
    dispatch gate, and the armed route now DISPATCHES the profile decision through
    the EXISTING ``_handle_exit_locked`` dispatcher. Run in dry_run so a square_off
    books a PAPER close and places NO real order — the route is proven end-to-end
    without touching the broker.

    Bid 2.10 < profile stop 2.25 (entry 3.0, initial_stop_pct 0.25) so the profile
    ladder produces a SQUARE_OFF; with the existing resting stop at 2.00 the EXISTING
    path does not exit, isolating the routed profile dispatch."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    # drives_live=True (the one-line flip) AND shadow_only=False so the gate's shadow
    # precondition is satisfied; position_source live_open; REAL runtime mode
    # live_approval_gated -> the gate OPENS.
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated")
    assert dep.exit.profile_exit_drives_live is True
    assert dep.execution.runtime_mode == "live_approval_gated"
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    # dry_run=True keeps the dispatch on the PAPER path (no real order placed).
    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    # The gate OPENED: dispatch_allowed True, mode flips to live_dispatch.
    assert shadow[0]["dispatch_allowed"] is True
    assert shadow[0]["mode"] == "live_dispatch"
    assert shadow[0]["exit"] is True
    # The route DISPATCHED through the existing handler: a routed audit event + a
    # real ExitPlan (the existing square_off path), and the position is CLOSED.
    routed = _events_of_type(repo, "profile_exit_dispatch_routed")
    assert len(routed) == 1
    assert routed[0]["fsm_action"] == "square_off"
    assert routed[0]["action"] == "square_off"
    assert routed[0]["dry_run"] is True
    exit_plans = _events_of_type(repo, "exit_plan")
    assert len(exit_plans) == 1
    assert exit_plans[0]["dry_run"] is True
    assert exit_plans[0]["action"] == "square_off"
    # The position was fully closed by the route (manage returns None; tracker empty).
    assert managed is None
    assert sup.planner.position_tracker.active_positions() == []
    # CRITICAL: NO real broker order was placed (dry_run paper close only).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    assert ("place_close", OPTION, 2, "STRATEGY") not in om.calls


def test_flip_seam_stays_closed_for_nonlive_position_source_even_with_flag_on(tmp_path) -> None:
    """Defense: even with the flag ON AND a real live_approval_gated runtime mode,
    a non-live position source (shadow) keeps the fail-closed allowlist CLOSED —
    the gate needs a live entry source too. Setting the real runtime mode isolates
    the position-source check (so this is not merely failing on a None mode)."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated")
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
# HIGH-1: the dispatch gate reads the deployment's REAL runtime mode, not a
# hardcoded ``live_approval_gated`` literal. A deployment running ``live_automated``
# (or shadow / advisory / unknown / None) can NEVER dispatch a profile exit, even
# with the operator flag ON and a live position source.
# --------------------------------------------------------------------------- #


def test_high1_live_automated_deployment_gate_stays_closed_even_with_flag_on(tmp_path) -> None:
    """A deployment whose REAL runtime mode is ``live_automated`` MUST NOT be able
    to dispatch a profile exit, even with the operator flag ON, a live source, and
    a profile rung that fires. ``live_automated`` is forbidden by the fail-closed
    allowlist (``DISPATCH_ALLOWED_RUNTIME_MODES = {live_approval_gated}``)."""
    om = _CallRecordingOrderManager(bid=2.10)  # below profile stop 2.25 -> would exit
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_automated")
    assert dep.exit.profile_exit_drives_live is True
    assert dep.execution.runtime_mode == "live_automated"
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    # The profile ladder WOULD exit, but the gate is CLOSED on the runtime mode.
    assert shadow[0]["exit"] is True
    assert shadow[0]["dispatch_allowed"] is False
    assert shadow[0]["mode"] == "shadow_record"
    # Gate shut -> the armed route never fired and the native path did NOT yield.
    assert _events_of_type(repo, "profile_exit_dispatch_routed") == []
    assert _events_of_type(repo, "native_exit_yielded_to_profile") == []
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    assert managed is not None and managed.exit_mode is None and managed.exit_order_id is None


def test_high1_unset_runtime_mode_fails_closed_even_with_flag_on(tmp_path) -> None:
    """A deployment that declares NO runtime mode (``None``) fails closed: the gate
    stays shut even with the flag ON and a live source. The wiring never substitutes
    a permissive default for a missing mode (the prior hardcoded-literal bug)."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True, runtime_mode=None)
    assert dep.execution.runtime_mode is None
    assert sup._resolved_runtime_mode(dep) is None  # the real source resolves to None
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["dispatch_allowed"] is False
    assert _events_of_type(repo, "profile_exit_dispatch_routed") == []
    assert _events_of_type(repo, "native_exit_yielded_to_profile") == []
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_high1_shadow_runtime_mode_fails_closed_even_with_flag_on(tmp_path) -> None:
    """A deployment whose real runtime mode is ``shadow`` also fails closed (the
    gate's lone permitted mode is ``live_approval_gated``)."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True, runtime_mode="shadow")
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["dispatch_allowed"] is False
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_high1_live_approval_gated_with_flag_on_and_live_source_opens_gate(tmp_path) -> None:
    """The positive control for HIGH-1: a deployment whose REAL runtime mode IS
    ``live_approval_gated`` (the lone permitted mode), flag ON, live source, opens
    the gate. (Run dry_run so the now-armed route dispatches on the paper path; the
    routed dispatch itself is proven in the armed-route test.)"""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated")
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0)

    asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["dispatch_allowed"] is True
    assert shadow[0]["mode"] == "live_dispatch"
    # No real broker order (paper dispatch only).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


# --------------------------------------------------------------------------- #
# MEDIUM-1(flip): shadow-advanced state must not corrupt a mid-position flip.
# With the flag OFF the recorder advances the persisted ladder every tick (banks
# T1, arms breakeven) without placing anything. If the operator flips the flag ON
# mid-position the now-live evaluator must NOT inherit that shadow-banked partial
# (which would under-size the live exit and skip the breakeven): the state is
# reseeded fresh from the current premium on the first live tick. A position that
# has only ever run live is unaffected.
# --------------------------------------------------------------------------- #


def _partial_t1_profile_deployment(*, drives_live: bool, runtime_mode: str | None = None):
    """Profile deployment with a PARTIAL T1 (banks half at T1) so a shadow-advanced
    state visibly differs from a clean one (banked_quantity 1 vs 0 on a 2-lot, and
    the active stop is breakeven vs the initial stop)."""
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = base.exit.model_copy(
        update={
            "profile_exit_id": "FLASH_REVERSAL",
            "profile_exit_shadow_only": True,
            "profile_exit_drives_live": drives_live,
            "target_1_r": 1.0,
            "target_2_r": 5.0,  # far out of reach so T2 never fires in these tests
            "target_1_quantity": 0.5,  # PARTIAL: bank 1 of 2 at T1
            "initial_stop_pct": 0.25,
            "premium_disaster_stop_pct": 0.30,
            "high_water_giveback_policy": "OFF",
            "breakeven_after_t1": True,
            "eod_flat": False,
        }
    )
    execution = base.execution.model_copy(update={"runtime_mode": runtime_mode})
    return DeploymentManifest(
        deployment_id=base.deployment_id,
        enabled=True,
        symbol=base.symbol,
        strategy=base.strategy,
        execution=execution,
        risk=base.risk,
        exit=exit_spec,
        source=base.source,
    )


def test_flip_midposition_reseeds_shadow_banked_state_so_live_exit_is_full_size(tmp_path) -> None:
    """MEDIUM-1(flip), case (i): the flag flips ON mid-position AFTER a partial T1
    was banked in shadow. The now-live evaluator must NOT treat the partial as
    already banked — it reseeds and respects the FULL size, with the breakeven flag
    cleared.

    Setup (entry 3.0, qty 2, target_1_r 1.0, initial_stop_pct 0.25 -> R=0.75,
    T1 price 3.75, initial stop price 2.25):
      * Shadow tick 1 @ bid 4.0 (>=3.75): banks partial T1 -> target_1_banked True,
        banked_quantity 1, stop_at_breakeven True.
      * Shadow tick 2 @ bid 4.0: emits STOP_TO_BREAKEVEN -> breakeven_emitted True.
      * Flip ON (live_approval_gated, live source). Live tick @ bid 2.10:
        - If state were INHERITED (bug): active stop is BREAKEVEN (entry 3.0); since
          2.10 <= 3.0 it would square off only the REMAINING 1 lot -> under-sized,
          and the breakeven was already emitted.
        - With the RESEED (fix): the ladder is clean, active stop is the INITIAL
          stop (2.25); 2.10 <= 2.25 -> a FULL 2-lot initial_stop exit.
    """
    # --- shadow phase: bank the partial T1 and emit breakeven (flag OFF) ---
    # The EXISTING resting stop is at 2.00 (below the profile's initial stop 2.25)
    # so the existing management path never exits in either phase (4.0 > 2.00 and
    # 2.10 > 2.00), isolating the profile-branch behavior under test.
    om_shadow = _CallRecordingOrderManager(bid=4.0)
    sup, repo = _supervisor(tmp_path, om_shadow)
    dep_shadow = _partial_t1_profile_deployment(drives_live=False)
    pos = _open_live_position(sup, dep_shadow, stop_order_id="STOP_OLD", stop_price=2.00, entry=3.0, quantity=2)
    key = sup._profile_state_key(pos)

    asyncio.run(sup.manage_open_position(dep_shadow, pos, dry_run=False))
    pos2 = sup.planner.position_tracker.active_positions()[0]
    asyncio.run(sup.manage_open_position(dep_shadow, pos2, dry_run=False))

    # The shadow ladder DID advance: partial banked + breakeven emitted, marked
    # shadow-advanced. (Proves the corruption precondition really exists.)
    state = sup._profile_exit_states[key]
    assert state.target_1_banked is True
    assert state.banked_quantity == 1
    assert state.breakeven_emitted is True
    assert state.shadow_advanced is True
    assert state.target_1_premium == 4.0  # the shadow-banked partial premium
    # ...but NO order was ever placed in shadow (record-only).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om_shadow.calls)

    # --- flip ON mid-position: same deployment_id/trade -> same persisted state ---
    # Swap in a low-premium quote (2.10 < the profile initial stop 2.25 but still
    # > the existing resting stop 2.00) for the now-live tick.
    om_live = _CallRecordingOrderManager(bid=2.10)
    sup.planner.order_manager = om_live
    dep_live = _partial_t1_profile_deployment(drives_live=True, runtime_mode="live_approval_gated")
    pos3 = sup.planner.position_tracker.active_positions()[0]

    # dry_run=True keeps the now-armed live dispatch on the PAPER path (no real
    # order). The gate ignores dry_run, so it still opens.
    managed_live = asyncio.run(sup.manage_open_position(dep_live, pos3, dry_run=True))

    live_shadow = [e for e in _events_of_type(repo, "profile_exit_shadow")][-1]
    # The gate is OPEN (real live mode + flag + live source)...
    assert live_shadow["dispatch_allowed"] is True
    assert live_shadow["mode"] == "live_dispatch"
    # ...and the live decision is the FULL-size initial stop, NOT an under-sized
    # breakeven exit of the remaining 1 lot. This is the crux of the fix: the
    # reseed wiped the shadow-banked partial so the live exit respects the FULL
    # size. (Proven via the recorded decision; the armed route then closes the
    # position, which clears the in-memory ladder state, so the reseed is asserted
    # here on the emitted decision rather than post-close state.)
    assert live_shadow["rule"] == "initial_stop"
    assert live_shadow["reason"] == "profile_initial_stop"
    assert live_shadow["exit"] is True
    assert live_shadow["exit_quantity"] == 2  # FULL size respected (reseed worked)

    # The armed route DISPATCHED the full-size close through the existing handler
    # (paper, no real order) and closed the position.
    routed = _events_of_type(repo, "profile_exit_dispatch_routed")
    assert len(routed) == 1 and routed[0]["fsm_action"] == "square_off"
    assert managed_live is None
    assert sup.planner.position_tracker.active_positions() == []
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om_live.calls)
    # Ladder state cleared on the terminal close.
    assert key not in sup._profile_exit_states


def test_live_from_entry_partial_t1_is_unaffected_by_reseed_guard(tmp_path) -> None:
    """MEDIUM-1(flip), case (ii): a position that has ONLY ever run live (clean from
    entry) is unaffected by the reseed guard — its partial T1 banks and persists
    normally across live ticks (no spurious reseed wiping the banked partial)."""
    om = _CallRecordingOrderManager(bid=4.0)  # >= T1 price 3.75 -> banks partial
    sup, repo = _supervisor(tmp_path, om)
    dep = _partial_t1_profile_deployment(drives_live=True, runtime_mode="live_approval_gated")
    pos = _open_live_position(sup, dep, stop_order_id="STOP_OLD", stop_price=2.25, entry=3.0, quantity=2)
    key = sup._profile_state_key(pos)

    # Tick 1 (live): banks the partial T1. dry_run=True keeps the now-armed partial
    # dispatch on the PAPER path (no real order); the gate ignores dry_run.
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))
    state_after_t1 = sup._profile_exit_states[key]
    assert state_after_t1.target_1_banked is True
    assert state_after_t1.banked_quantity == 1
    # Never shadow-advanced (always live) -> the reseed guard never triggers.
    assert state_after_t1.shadow_advanced is False

    first = _events_of_type(repo, "profile_exit_shadow")[0]
    assert first["rule"] == "target_1_partial"
    assert first["exit_quantity"] == 1  # the genuine live partial
    assert first["dispatch_allowed"] is True
    # The armed route dispatched the PARTIAL through the existing partial-scale
    # handler (paper) and left the residual runner OPEN (quantity 1, not closed).
    assert _events_of_type(repo, "profile_exit_dispatch_routed")[0]["fsm_action"] == "partial_scale"
    residual = sup.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1

    # Tick 2 (live, still elevated): the banked partial PERSISTS (no reseed wiped
    # it); the ladder emits the breakeven ratchet, not a fresh full T1.
    pos2 = sup.planner.position_tracker.active_positions()[0]
    asyncio.run(sup.manage_open_position(dep, pos2, dry_run=True))
    state_after_t2 = sup._profile_exit_states[key]
    assert state_after_t2.target_1_banked is True  # STILL banked (not reset)
    assert state_after_t2.banked_quantity == 1
    assert state_after_t2.breakeven_emitted is True
    assert state_after_t2.shadow_advanced is False
    # No real broker close/square-off order across either tick (paper only).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


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
