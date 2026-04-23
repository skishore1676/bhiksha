import asyncio

from bhiksha.config.loader import load_deployments
from bhiksha.config.models import AppConfig
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.repository import NullEventRepository
from bhiksha.state.position_tracker import PositionTracker


class SequenceOrderManager:
    supports_concurrent_exit_orders = False
    allows_exit_submission_before_cancel_confirmation = True

    def __init__(self, quotes: list[float]) -> None:
        self._quotes = list(quotes)
        self.calls: list[tuple[str, str | float]] = []

    async def get_option_quote(self, option_symbol: str) -> PublicQuote:
        if len(self._quotes) > 1:
            price = self._quotes.pop(0)
        else:
            price = self._quotes[0]
        return PublicQuote(
            symbol=option_symbol,
            bid=price,
            ask=price + 0.05,
            last=price + 0.02,
            open_interest=500,
            outcome="SUCCESS",
        )

    async def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        return True, None

    async def place_target_order(self, option_symbol: str, limit_price: float, quantity: int):
        self.calls.append(("place_target", round(limit_price, 2)))

        class Result:
            order_id = "TARGET123"
            error = None

        return Result()

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
        self.calls.append(("place_stop", round(stop_price, 2)))

        class Result:
            order_id = "STOP123"
            error = None

        return Result()


class SequencePlanner:
    def __init__(self, order_manager: SequenceOrderManager) -> None:
        self.order_manager = order_manager
        self.position_tracker = PositionTracker()

    async def close(self):
        return None


def _target_enabled_deployment():
    base = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    return base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_profit_target": True,
                    "profit_target_multiple": 1.5,
                    "target_approach_offset_pct": 0.02,
                    "target_pullback_restore_progress_pct": 0.8,
                }
            )
        }
    )


def test_position_lifecycle_replay_activates_target_then_restores_stop() -> None:
    deployment = _target_enabled_deployment()
    order_manager = SequenceOrderManager([3.10, 3.30, 3.00])
    supervisor = ExecutionSupervisor(
        planner=SequencePlanner(order_manager),
        event_repository=NullEventRepository(),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="broker_sync",
        stop_order_id="STOP123",
        stop_price=1.1,
        target_price=3.35,
    )

    initial = supervisor.planner.position_tracker.active_positions()[0]
    step1 = asyncio.run(supervisor.manage_open_position(deployment, initial, dry_run=False))
    assert step1 is not None
    assert step1.stop_order_id == "STOP123"
    assert step1.target_order_id is None

    step2 = asyncio.run(supervisor.manage_open_position(deployment, step1, dry_run=False))
    assert step2 is not None
    assert step2.stop_order_id is None
    assert step2.target_order_id == "TARGET123"

    step3 = asyncio.run(supervisor.manage_open_position(deployment, step2, dry_run=False))
    assert step3 is not None
    assert step3.stop_order_id == "STOP123"
    assert step3.target_order_id is None
    assert step3.stop_price == 1.1

    assert order_manager.calls == [
        ("cancel", "STOP123"),
        ("place_target", 3.35),
        ("cancel", "TARGET123"),
        ("place_stop", 1.1),
    ]


def test_position_lifecycle_replay_does_not_duplicate_live_target_after_restart() -> None:
    deployment = _target_enabled_deployment()
    order_manager = SequenceOrderManager([3.32])
    supervisor = ExecutionSupervisor(
        planner=SequencePlanner(order_manager),
        event_repository=NullEventRepository(),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    # Simulate a restarted runtime after reconciliation has already recovered a live target order.
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="broker_sync",
        stop_order_id=None,
        stop_price=1.1,
        target_order_id="TARGET999",
        target_price=3.35,
    )

    position = supervisor.planner.position_tracker.active_positions()[0]
    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.target_order_id == "TARGET999"
    assert managed.stop_order_id is None
    assert order_manager.calls == []


def test_position_lifecycle_replay_preserves_active_stop_before_target_approach() -> None:
    deployment = _target_enabled_deployment()
    order_manager = SequenceOrderManager([3.20, 3.22])
    supervisor = ExecutionSupervisor(
        planner=SequencePlanner(order_manager),
        event_repository=NullEventRepository(),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="broker_sync",
        stop_order_id="STOP123",
        stop_price=1.1,
        target_price=3.35,
    )

    position = supervisor.planner.position_tracker.active_positions()[0]
    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.stop_order_id == "STOP123"
    assert managed.target_order_id is None
    assert order_manager.calls == []


def test_position_lifecycle_replay_restores_missing_stop_after_reconciliation() -> None:
    deployment = _target_enabled_deployment()
    order_manager = SequenceOrderManager([3.10])
    supervisor = ExecutionSupervisor(
        planner=SequencePlanner(order_manager),
        event_repository=NullEventRepository(),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="recovered-trade",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="broker_recovered",
        stop_order_id=None,
        stop_price=None,
    )

    position = supervisor.planner.position_tracker.active_positions()[0]
    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.stop_order_id == "STOP123"
    assert managed.stop_price == 1.1
    assert ("place_stop", 1.1) in order_manager.calls
