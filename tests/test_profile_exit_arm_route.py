"""Phase 2: the ARMED profile-exit dispatch route.

These tests pin down the Phase-2 behavior: when (and ONLY when) the fail-closed
dispatch gate is OPEN, the profile-exit decision is DISPATCHED through bhiksha's
EXISTING exit machinery (``_handle_exit_locked`` and the locked handlers it
delegates to) — inheriting the same locking / idempotency / dry_run / order-
placement safety as a native exit. With the operator flag OFF (the default and
the only state shipped) the route stays DORMANT and production behavior is
unchanged. The double-exit / authority invariant (the native exit path yields to
the profile route whenever the gate is open) is proven directly.

Everything here runs in dry_run / shadow: NO real broker order is ever placed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from bhiksha.config.models import AppConfig, DeploymentManifest, ExitSpec
from bhiksha.domain.models import ExitDecision
from bhiksha.execution.order_manager import OrderResult, PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository

from test_execution_supervisor import (
    RecordingPlanner,
    StubOrderManager,
    _enabled_deployment,
    _events_of_type,
)

BASE_DEPLOYMENT_ID = "market_impulse_qqq_short_v1"
OPTION = "QQQ260401P00556000"


# --------------------------------------------------------------------------- #
# Fixtures — a profile deployment whose ladder we can steer onto each fsm_action
# --------------------------------------------------------------------------- #


class _CallRecordingOrderManager(StubOrderManager):
    """Records every broker-affecting call; quote drives the profile ladder."""

    def __init__(self, *, bid: float = 3.0) -> None:
        self.bid = bid
        self.calls: list[tuple] = []

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=self.bid,
            ask=self.bid + 0.05,
            last=self.bid + 0.02,
            open_interest=500,
            outcome="SUCCESS",
        )

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


def _profile_deployment(
    *,
    drives_live: bool,
    runtime_mode: str | None,
    target_1_quantity: float = 1.0,
    target_2_r: float = 2.0,
):
    """A v2 operator exit profile pinned onto the base deployment.

    ``target_1_quantity`` 1.0 -> a full SQUARE_OFF at T1; 0.5 -> a PARTIAL_SCALE
    (bank 1 of a 2-lot) which arms breakeven for a later STOP_TO_BREAKEVEN tick.
    """
    base = _enabled_deployment(BASE_DEPLOYMENT_ID)
    exit_spec = base.exit.model_copy(
        update={
            "profile_exit_id": "FLASH_REVERSAL",
            "profile_exit_shadow_only": True,
            "profile_exit_drives_live": drives_live,
            "target_1_r": 1.0,
            "target_2_r": target_2_r,
            "target_1_quantity": target_1_quantity,
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


def _open_live_position(supervisor, deployment, *, stop_order_id="STOP_OLD", stop_price=2.00, quantity=2, entry=3.0):
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


def _spy_on_primitives(monkeypatch, supervisor):
    """Wrap the three exit primitives so a test can assert WHICH one fired."""
    seen: dict[str, int] = {"replacement_stop": 0, "partial_scale": 0, "full_close": 0}

    orig_repl = supervisor._apply_replacement_stop
    orig_partial = supervisor._handle_partial_scale_locked
    orig_submit = supervisor._submit_exit_request

    async def repl(*args, **kwargs):
        seen["replacement_stop"] += 1
        return await orig_repl(*args, **kwargs)

    async def partial(*args, **kwargs):
        seen["partial_scale"] += 1
        return await orig_partial(*args, **kwargs)

    async def submit(*args, **kwargs):
        # ``_submit_exit_request`` is the LIVE full-close submission path; in dry_run
        # the full close goes through the paper branch and never calls this, so we
        # count full closes from the dry_run paper path via the exit_plan event too.
        seen["full_close"] += 1
        return await orig_submit(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_apply_replacement_stop", repl)
    monkeypatch.setattr(supervisor, "_handle_partial_scale_locked", partial)
    monkeypatch.setattr(supervisor, "_submit_exit_request", submit)
    return seen


# --------------------------------------------------------------------------- #
# 1. ARMED, gate forced open: each fsm_action routes to the CORRECT existing
#    primitive, in dry_run, placing NO real order.
# --------------------------------------------------------------------------- #


def test_armed_square_off_routes_to_full_close_no_real_order(tmp_path, monkeypatch) -> None:
    """SQUARE_OFF -> the existing full-close path. Bid 2.10 < profile initial stop
    2.25 (entry 3.0) -> initial_stop SQUARE_OFF; existing resting stop at 2.00 so
    the native management path does not itself exit. dry_run -> paper close."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    seen = _spy_on_primitives(monkeypatch, sup)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert shadow[-1]["dispatch_allowed"] is True
    assert shadow[-1]["rule"] == "initial_stop"
    # Routed through the FULL-CLOSE path (square_off action) and the position closed.
    routed = _events_of_type(repo, "profile_exit_dispatch_routed")
    assert len(routed) == 1 and routed[0]["action"] == "square_off"
    assert routed[0]["fsm_action"] == "square_off"
    exit_plans = _events_of_type(repo, "exit_plan")
    assert len(exit_plans) == 1 and exit_plans[0]["action"] == "square_off" and exit_plans[0]["dry_run"] is True
    assert managed is None
    assert sup.planner.position_tracker.active_positions() == []
    # The full close went through the dry_run PAPER branch (not the partial or
    # breakeven primitives) — and placed NO real order.
    assert seen["replacement_stop"] == 0
    assert seen["partial_scale"] == 0
    assert seen["full_close"] == 0  # dry_run full close uses the paper branch
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_armed_partial_scale_routes_to_partial_handler_no_real_order(tmp_path, monkeypatch) -> None:
    """PARTIAL_SCALE -> the existing ``_handle_partial_scale_locked``. T1 at price
    3.75 (R=0.75) with target_1_quantity 0.5 banks 1 of 2; bid 4.0 fires it. The
    residual runner stays OPEN. dry_run -> no real order."""
    om = _CallRecordingOrderManager(bid=4.0)
    sup, repo = _supervisor(tmp_path, om)
    seen = _spy_on_primitives(monkeypatch, sup)
    dep = _profile_deployment(
        drives_live=True, runtime_mode="live_approval_gated", target_1_quantity=0.5, target_2_r=5.0
    )
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))

    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert shadow[-1]["dispatch_allowed"] is True
    assert shadow[-1]["rule"] == "target_1_partial"
    assert shadow[-1]["exit_quantity"] == 1
    routed = _events_of_type(repo, "profile_exit_dispatch_routed")
    assert len(routed) == 1 and routed[0]["fsm_action"] == "partial_scale"
    # Routed to the PARTIAL handler specifically (not full close / not breakeven).
    assert seen["partial_scale"] == 1
    assert seen["full_close"] == 0
    assert seen["replacement_stop"] == 0
    # The partial-scale handler ran (its dedicated event) and left a residual open.
    assert len(_events_of_type(repo, "partial_scale_submission")) == 1
    assert managed is not None and managed.quantity == 1
    assert sup.planner.position_tracker.active_positions()[0].quantity == 1
    # dry_run -> no real broker close/square-off order.
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_armed_breakeven_routes_to_replacement_stop_not_an_exit(tmp_path, monkeypatch) -> None:
    """STOP_TO_BREAKEVEN -> the existing ``_apply_replacement_stop`` (tighten the
    stop), NOT an exit order. After a partial T1 banks and arms breakeven, the next
    elevated tick emits STOP_TO_BREAKEVEN. The position stays OPEN; only the stop
    moves to entry. dry_run -> no real order."""
    om = _CallRecordingOrderManager(bid=4.0)  # >= T1 price 3.75
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(
        drives_live=True, runtime_mode="live_approval_gated", target_1_quantity=0.5, target_2_r=5.0
    )
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    # Tick 1: banks the partial T1 (arms the breakeven ratchet).
    asyncio.run(sup.manage_open_position(dep, pos, dry_run=True))
    # Spy only on tick 2 so the partial from tick 1 does not pollute the counts.
    seen = _spy_on_primitives(monkeypatch, sup)
    pos2 = sup.planner.position_tracker.active_positions()[0]

    # Tick 2: emits STOP_TO_BREAKEVEN -> routes to the replacement-stop primitive.
    managed = asyncio.run(sup.manage_open_position(dep, pos2, dry_run=True))

    shadow = _events_of_type(repo, "profile_exit_shadow")[-1]
    assert shadow["dispatch_allowed"] is True
    assert shadow["fsm_action"] == "stop_to_breakeven"
    assert shadow["exit"] is False  # a stop move, not an exit
    routed = _events_of_type(repo, "profile_exit_dispatch_routed")[-1]
    assert routed["fsm_action"] == "stop_to_breakeven"
    assert routed["action"] == "hold"
    # Routed to the REPLACEMENT-STOP primitive only (no exit/close).
    assert seen["replacement_stop"] == 1
    assert seen["partial_scale"] == 0
    assert seen["full_close"] == 0
    assert len(_events_of_type(repo, "profile_replacement_stop")) == 1
    # The position is STILL OPEN (not exited), and the stop moved to entry (3.0).
    assert managed is not None
    assert sup.planner.position_tracker.active_positions()[0].quantity == 1
    assert managed.stop_price == pytest.approx(3.0)
    # No exit/close order on either path (a hold-class breakeven never exits).
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


# --------------------------------------------------------------------------- #
# 2. DORMANCY: with the operator flag OFF (the default) the route never
#    dispatches — 0 calls into ANY exit primitive — proving production is
#    unchanged, even when the profile ladder WOULD exit.
# --------------------------------------------------------------------------- #


def test_dormant_flag_off_never_dispatches_even_when_ladder_would_exit(tmp_path, monkeypatch) -> None:
    """The DORMANCY proof. Flag OFF (default). Bid 2.10 makes the profile ladder
    produce a SQUARE_OFF in shadow, but the gate is SHUT so the route NEVER calls
    any exit primitive, places NO order, and leaves the position open & unchanged.
    Run with dry_run=False to mirror a real live management tick — proving the
    dormancy holds even on the live path."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    seen = _spy_on_primitives(monkeypatch, sup)
    # Flag OFF, but live_approval_gated + live source so ONLY the flag keeps the
    # gate shut (isolates the operator flip as the sole gate).
    dep = _profile_deployment(drives_live=False, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    assert dep.exit.profile_exit_drives_live is False
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    managed = asyncio.run(sup.manage_open_position(dep, pos, dry_run=False))

    # The ladder recorded a would-exit decision in shadow...
    shadow = _events_of_type(repo, "profile_exit_shadow")
    assert len(shadow) == 1
    assert shadow[0]["exit"] is True
    assert shadow[0]["dispatch_allowed"] is False
    assert shadow[0]["mode"] == "shadow_record"
    # ...but ZERO dispatch: no routed event, no exit primitive called, no order.
    assert _events_of_type(repo, "profile_exit_dispatch_routed") == []
    assert seen == {"replacement_stop": 0, "partial_scale": 0, "full_close": 0}
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    assert _events_of_type(repo, "exit_plan") == []
    # The position is untouched and still open.
    assert managed is not None
    assert managed.quantity == 2
    assert managed.exit_mode is None and managed.exit_order_id is None


def test_dormant_authority_predicate_is_false_with_flag_off(tmp_path) -> None:
    """The authority predicate (which makes the native path yield) is FALSE with
    the flag OFF, so the native exit path keeps full authority in production."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, _ = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=False, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    pos = _open_live_position(sup, dep)
    assert sup._profile_exit_is_authoritative(dep, pos) is False


# --------------------------------------------------------------------------- #
# 3. DOUBLE-EXIT / AUTHORITY INVARIANT: when the profile route is the authority
#    (gate open) the NATIVE exit path YIELDS — no conflicting double action.
# --------------------------------------------------------------------------- #


def _native_square_off_decision(deployment) -> ExitDecision:
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime.now(UTC),
        exit=True,
        action="square_off",
        reason=["native_stop_loss"],
        cancel_protection_orders=True,
    )


def test_native_exit_yields_when_profile_route_is_authoritative(tmp_path, monkeypatch) -> None:
    """When the gate is OPEN (flag ON + live_approval_gated + live source) the
    profile route is the SOLE exit authority. A native exit arriving for the SAME
    position (e.g. the same tick's stop-loss exit task, carrying its pre-manage
    snapshot) must YIELD: ``handle_exit`` returns None, calls NO exit primitive,
    places NO order, and records a ``native_exit_yielded_to_profile`` audit event.
    This is the double-exit guard — the two authorities can never both act."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    seen = _spy_on_primitives(monkeypatch, sup)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    # The profile route IS authoritative for this position.
    assert sup._profile_exit_is_authoritative(dep, pos) is True

    # A native exit lands via the public lock-acquiring entry (as the runtime does).
    plan = asyncio.run(sup.handle_exit(dep, pos, _native_square_off_decision(dep), dry_run=True))

    # The native path YIELDED: no plan, no primitive, no order; just the audit event.
    assert plan is None
    assert seen == {"replacement_stop": 0, "partial_scale": 0, "full_close": 0}
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)
    yielded = _events_of_type(repo, "native_exit_yielded_to_profile")
    assert len(yielded) == 1 and yielded[0]["action"] == "square_off"
    # The native exit did NOT close the position (the profile route owns that).
    assert sup.planner.position_tracker.active_positions()[0].quantity == 2
    assert _events_of_type(repo, "exit_plan") == []


def test_native_exit_acts_normally_when_profile_not_authoritative(tmp_path, monkeypatch) -> None:
    """Control for the authority guard: with the flag OFF the profile route is NOT
    authoritative, so a native exit goes through ``handle_exit`` -> the existing
    dispatcher normally (here a dry_run paper square_off), and NO yield event is
    recorded. Proves the guard does not over-suppress native exits."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    dep = _profile_deployment(drives_live=False, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    assert sup._profile_exit_is_authoritative(dep, pos) is False

    plan = asyncio.run(sup.handle_exit(dep, pos, _native_square_off_decision(dep), dry_run=True))

    # The native exit acted (paper close) — no yield, position closed.
    assert _events_of_type(repo, "native_exit_yielded_to_profile") == []
    assert plan is not None and plan.action == "square_off" and plan.dry_run is True
    assert sup.planner.position_tracker.active_positions() == []
    # dry_run -> still no real broker order.
    assert not any(c[0] in {"place_close", "place_square_off"} for c in om.calls)


def test_native_breakeven_deployment_yields_to_profile_when_authoritative(tmp_path, monkeypatch) -> None:
    """Authority is TOTAL while the gate is open: even a native hold-class stop move
    (the existing breakeven-after-R promotion) must not fight the profile route. A
    native ``handle_exit`` carrying a replacement_stop also yields when the profile
    route is authoritative (no fighting stops)."""
    om = _CallRecordingOrderManager(bid=2.10)
    sup, repo = _supervisor(tmp_path, om)
    seen = _spy_on_primitives(monkeypatch, sup)
    dep = _profile_deployment(drives_live=True, runtime_mode="live_approval_gated", target_1_quantity=1.0)
    pos = _open_live_position(sup, dep, stop_price=2.00, entry=3.0, quantity=2)

    native_stop_move = ExitDecision(
        deployment_id=dep.deployment_id,
        symbol="QQQ",
        timestamp=datetime.now(UTC),
        exit=False,
        action="hold",
        reason=["native_breakeven"],
        replacement_stop_price=3.0,
    )
    plan = asyncio.run(sup.handle_exit(dep, pos, native_stop_move, dry_run=True))

    assert plan is None
    # No stop move was applied by the native path (the profile route owns the stop).
    assert seen["replacement_stop"] == 0
    assert _events_of_type(repo, "profile_replacement_stop") == []
    assert len(_events_of_type(repo, "native_exit_yielded_to_profile")) == 1
