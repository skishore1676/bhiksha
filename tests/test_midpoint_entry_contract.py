"""Trader scenarios through the real planner, broker adapter and lifecycle owners."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from bhiksha.config.models import AppConfig, ActivePlan
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision
from bhiksha.execution.order_manager import OrderManager
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository, SQLiteTradeStateRepository
from test_execution_planner import StubChainService, StubRiskManager, _enabled_deployment

NOW = datetime(2026, 10, 8, 14, tzinfo=UTC)


class Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    Clock.current = NOW
    for module in ("planner", "pricing", "supervisor"):
        monkeypatch.setattr(f"bhiksha.execution.{module}.datetime", Clock)
    monkeypatch.setattr("bhiksha.execution.order_manager.datetime", Clock)


def lane(*, cap=600, retries=600):
    dep = _enabled_deployment("market_impulse_qqq_short_v1")
    dep.risk = dep.risk.model_copy(update={"max_trade_premium_usd": cap, "max_contracts": 5})
    dep.execution = dep.execution.model_copy(update={
        "entry_execution_profile": "balanced", "entry_reprice_checkpoints_seconds": [30],
        "entry_liquidity_retry_seconds": retries, "entry_liquidity_retry_interval_seconds": 60,
        "entry_window_start_et": "09:30", "entry_window_end_et": "15:45",
    })
    return dep


def signal(dep):
    return SignalDecision(dep.deployment_id, dep.symbol, Clock.current, True,
                          SignalDirection.SHORT, ["qualified_completed_bar"], {"close": 558})


class Broker:
    """No network or money: preserve Public's real request/response interfaces."""
    def __init__(self, bid=2.7, ask=2.9, tick=.05):
        self.bid, self.ask, self.tick = bid, ask, tick
        self.buy_orders = []
        self.cancelled = []
        self.quote_symbols = []
        self.preflight_failures = 0
        self.preflight_requests = []
        self.submit_failure = None
        self.cancel_status = "CANCELLED"
        self.cancel_fill = 0
        self.status_override = None
        self.intent_repo = None

    async def get_quotes(self, instruments):
        symbol = instruments[0]["symbol"]
        self.quote_symbols.append(symbol)
        bid, ask = (self.bid, self.ask)
        if hasattr(self, "markets"):
            bid, ask = self.markets[symbol]
        return {"quotes": [{"instrument": {"symbol": symbol}, "bid": bid, "ask": ask,
            "openInterest": 500, "quoteTimestamp": Clock.current.isoformat(), "outcome": "SUCCESS"}]}

    async def preflight_single_leg(self, payload):
        self.preflight_requests.append(dict(payload))
        if self.preflight_failures:
            self.preflight_failures -= 1
            raise TimeoutError("temporary preflight transport failure")
        result = {"estimatedCost": float(payload["limitPrice"]) * int(payload["quantity"]) * 100 + .02}
        if self.tick:
            result["priceIncrement"] = {"currentIncrement": self.tick}
        return result

    async def place_order(self, payload):
        if payload["orderSide"] == "BUY":
            if self.intent_repo:
                records = await self.intent_repo.get_open_trades()
                assert any(r.entry_order_id == payload["orderId"] and r.status == "pending_entry_reconcile"
                           for r in records), "BUY identity must be durable before broker I/O"
            self.buy_orders.append(dict(payload))
            if self.submit_failure:
                raise self.submit_failure
        return {"orderId": payload["orderId"]}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def get_order(self, order_id):
        if self.status_override is not None:
            return self.status_override
        return {"status": self.cancel_status if order_id in self.cancelled else "WORKING",
                "filledQuantity": self.cancel_fill, "quantity": 5, "averagePrice": .45}

    async def get_account_info(self):
        return {"brokerageAccountType": "CASH"}

    async def get_portfolio(self):
        return {"buyingPower": {"cashOnlyBuyingPower": "10000"}, "positions": [], "orders": []}

    async def close(self):
        pass


def owners(tmp_path, broker, *, risk=None, chain=None):
    db = str(tmp_path / "trades.db")
    trades, events = SQLiteTradeStateRepository(db), SQLiteEventRepository(db)
    broker.intent_repo = trades
    planner = ExecutionPlanner(chain_service=chain or StubChainService(), order_manager=OrderManager(broker=broker),
        risk_manager=risk, entry_intent_repository=trades)
    supervisor = ExecutionSupervisor(planner=planner, trade_state_repository=trades,
        event_repository=events, record_signal_outcomes=True)
    return planner, supervisor, trades


@pytest.mark.parametrize("bid,ask,cap,opening,replacement,quantity", [
    (.35, .89, 200, .45, .60, 2),       # RBLX: the bargain bid does not own the ceiling.
    (3.59, 3.69, 1000, 3.60, 3.65, 2), # SMH: 3.64 midpoint must respect the five-cent tick.
    (2.70, 2.90, 600, 2.80, 2.90, 1),  # Two opening contracts cannot fund the whole permitted move.
])
def test_live_and_shadow_share_price_quantity_tick_and_one_step(tmp_path, bid, ask, cap, opening, replacement, quantity):
    async def run():
        broker = Broker(bid, ask)
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane(cap=cap)
        live = await planner.plan_entry(dep, signal(dep), dry_run=False)
        assert live.quantity == quantity and live.estimated_entry_price == opening
        initial = dict(live.risk_details["entry_pricing"])
        assert quantity * initial["max_entry_price"] * 100 <= cap
        # Use a separate modeled book; no real BUY is permitted in this path.
        model_planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=OrderManager(broker=broker))
        paper = await model_planner.plan_entry(dep, signal(dep), dry_run=True, simulate_only=True)
        assert (paper.quantity, paper.estimated_entry_price) == (quantity, opening)
        assert paper.risk_details["entry_pricing"]["max_entry_price"] == initial["max_entry_price"]
        model = ExecutionSupervisor(planner=model_planner)
        model._paper_entries[paper.trade_id] = (dep, signal(dep), paper, NOW, NOW + timedelta(seconds=150))
        model.lifecycle_store.begin_entry(dep.symbol, dep.deployment_id, order_id="PAPER_PENDING")
        Clock.current = NOW + timedelta(seconds=31)
        result = await supervisor._reprice_live_entry(live, dep, attempt=1)
        await model.poll_paper_entries(now=Clock.current)
        modeled = model._paper_entries[paper.trade_id][2]
        assert result.plan.estimated_entry_price == modeled.estimated_entry_price == replacement
        assert result.plan.quantity == modeled.quantity == quantity
        assert len(broker.buy_orders) == 2
        assert model.planner.position_tracker.active_positions() == []
        # A fresh more expensive market cannot buy a second replacement.
        broker.bid, broker.ask = ask, ask + .10
        Clock.current += timedelta(seconds=31)
        second = await supervisor._reprice_live_entry(result.plan, dep, attempt=2)
        await model.poll_paper_entries(now=Clock.current)
        assert second.plan.order_id == result.plan.order_id and len(broker.buy_orders) == 2
        assert model._paper_entries[paper.trade_id][2].estimated_entry_price == replacement
        # Only a later usable ask-touch can establish the modeled fill.
        broker.bid, broker.ask = max(.01, replacement-.05), replacement
        Clock.current += timedelta(seconds=1)
        await model.poll_paper_entries(now=Clock.current)
        position = model.planner.position_tracker.active_positions()[0]
        assert position.source == "shadow" and position.quantity == quantity
        assert len(broker.buy_orders) == 2
    asyncio.run(run())


def test_wide_market_chooses_a_fresh_narrower_contract_in_same_cohort(tmp_path):
    class Chain(StubChainService):
        async def get_chain(self, *args, **kwargs):
            first = (await super().get_chain(*args, **kwargs))[0]
            return [replace(first, delta=-.30), replace(first, option_symbol="QQQ260330P00557000", delta=-.31)]
    async def run():
        broker = Broker()
        broker.markets = {"QQQ260330P00558000": (.35, .89), "QQQ260330P00557000": (.50, .55)}
        planner, _, _ = owners(tmp_path, broker, chain=Chain())
        plan = await planner.plan_entry(lane(cap=200), signal(lane()), dry_run=True, simulate_only=True)
        assert plan.option_symbol == "QQQ260330P00557000"
        assert plan.risk_details["entry_pricing"]["wide_market"] is False
        assert len(set(broker.quote_symbols)) == 2 and not broker.buy_orders
    asyncio.run(run())


@pytest.mark.parametrize("retries", [0, 600])
def test_temporary_reprice_failure_keeps_original_order_and_deadline(tmp_path, retries):
    async def run():
        broker = Broker()
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane(retries=retries)
        plan = await planner.plan_entry(dep, signal(dep), dry_run=False)
        deadline = NOW + timedelta(seconds=150)
        plan.risk_details["entry_working_deadline"] = deadline.isoformat()
        broker.preflight_failures = 1
        Clock.current += timedelta(seconds=31)
        failed = await supervisor._reprice_live_entry(plan, dep, attempt=1)
        assert failed.retryable and failed.plan.order_id == plan.order_id
        assert not broker.cancelled and len(broker.buy_orders) == 1
        assert "entry_reprice_attempt" not in failed.plan.risk_details
        assert failed.plan.risk_details["entry_working_deadline"] == deadline.isoformat()
        if retries:
            Clock.current += timedelta(seconds=60)
            recovered = await supervisor._reprice_live_entry(failed.plan, dep, attempt=1)
            assert recovered.plan.risk_details["entry_reprice_attempt"] == 1
            assert len(broker.buy_orders) == 2
        Clock.current = deadline
        expired = await supervisor._reprice_live_entry(failed.plan, dep, attempt=1)
        assert expired.cancelled_without_fill and expired.error == "entry_working_deadline_reached"
    asyncio.run(run())


@pytest.mark.parametrize("status,fill", [("CANCELLED", 1), ("WORKING", 0), ("CANCELLED", "unknown")])
def test_replacement_cancellation_partial_or_unknown_never_sends_second_buy(tmp_path, status, fill):
    async def run():
        broker = Broker()
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane(cap=2000)
        plan = await planner.plan_entry(dep, signal(dep), dry_run=False)
        assert plan.quantity == 5
        broker.cancel_status, broker.cancel_fill = status, fill
        Clock.current += timedelta(seconds=31)
        result = await supervisor._reprice_live_entry(plan, dep, attempt=1)
        assert len(broker.buy_orders) == 1
        if fill == 1:
            assert result.filled and result.plan.quantity == 1
        else:
            assert not result.cancelled_without_fill and result.error.startswith("entry_reprice_cancel_unconfirmed")
    asyncio.run(run())


@pytest.mark.parametrize("failure", [TimeoutError("unknown outcome"), None])
def test_uncertain_initial_buy_survives_restart_without_resubmission(tmp_path, failure):
    async def run():
        broker = Broker()
        broker.submit_failure = failure
        if failure is None:
            original = broker.place_order
            async def missing_id(payload):
                await original(payload)
                return {}
            broker.place_order = missing_id
        risk = StubRiskManager()
        planner, _, trades = owners(tmp_path, broker, risk=risk)
        dep = lane()
        plan = await planner.plan_entry(dep, signal(dep), dry_run=False)
        assert plan.order_id and plan.risk_details["entry_pricing"]["submission_uncertain"]
        assert risk.release_calls == [] and len(broker.buy_orders) == 1
        record = (await trades.get_open_trades())[0]
        assert record.entry_order_id == plan.order_id
        restarted = ExecutionSupervisor(planner=ExecutionPlanner(order_manager=OrderManager(broker=broker)),
                                        trade_state_repository=trades)
        assert await restarted._reconcile_pending_entry_release(record) == "pending"
        broker.status_override = {"status": "FILLED", "filledQuantity": plan.quantity,
                                  "quantity": plan.quantity, "averagePrice": plan.estimated_entry_price}
        assert await restarted._reconcile_pending_entry_release(record) == "recovered"
        assert len(broker.buy_orders) == 1
        assert restarted.planner.position_tracker.active_positions()[0].quantity == plan.quantity
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["initial", "replacement"])
def test_process_loss_after_buy_keeps_the_exact_durable_identity(tmp_path, stage):
    class ProcessLost(BaseException):
        pass
    async def run():
        broker = Broker()
        planner, supervisor, trades = owners(tmp_path, broker)
        dep = lane()
        if stage == "replacement":
            initial = await planner.plan_entry(dep, signal(dep), dry_run=False)
            Clock.current += timedelta(seconds=31)
        broker.submit_failure = ProcessLost()
        with pytest.raises(ProcessLost):
            if stage == "initial":
                await planner.plan_entry(dep, signal(dep), dry_run=False)
            else:
                await supervisor._reprice_live_entry(initial, dep, attempt=1)
        record = (await trades.get_open_trades())[0]
        assert record.entry_order_id == broker.buy_orders[-1]["orderId"]
        assert record.status == "pending_entry_reconcile"
        before = len(broker.buy_orders)
        restarted = ExecutionSupervisor(planner=ExecutionPlanner(order_manager=OrderManager(broker=broker)),
                                        trade_state_repository=trades)
        assert await restarted._reconcile_pending_entry_release(record) == "pending"
        assert len(broker.buy_orders) == before
    asyncio.run(run())


def test_proved_pre_submission_failure_cannot_enter_fill_denominator(tmp_path):
    from bhiksha.ops.exit_comparisons_sheet import _actual_fills
    async def run():
        broker = Broker()
        risk = StubRiskManager(allowed=False, reason="risk_canary_inhibited")
        planner, supervisor, trades = owners(tmp_path, broker, risk=risk)
        dep = lane()
        plan = await planner.plan_entry(dep, signal(dep), dry_run=False)
        await supervisor.event_repository.append("signal_outcome", {"outcome": "risk_block"})
        assert plan.order_id is None and not broker.buy_orders
        assert await trades.get_open_trades() == []
        assert _actual_fills(tmp_path / "trades.db", NOW.date())["broker_confirmed"] == 0
    asyncio.run(run())


def test_shadow_missing_tick_metadata_is_explicit_and_does_not_fill(tmp_path):
    async def run():
        broker = Broker(tick=None)
        planner, _, _ = owners(tmp_path, broker)
        dep = lane()
        plan = await planner.plan_entry(dep, signal(dep), dry_run=True, simulate_only=True)
        assert plan.order_id is None and "public_preflight_tick_metadata_unavailable" in plan.risk_reasons
        assert not plan.risk_details["pre_submission_retryable"] and not broker.buy_orders
    asyncio.run(run())


def test_initial_transport_recovery_freezes_quote_reference_and_original_signal_expiry(tmp_path):
    async def run():
        broker = Broker()
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane()
        broker.preflight_failures = 1
        first = await supervisor.handle_signal(dep, signal(dep), dry_run=False)
        assert first is None and not broker.buy_orders
        retry = supervisor._entry_liquidity_retries[dep.deployment_id]
        original_id = retry.opportunity_id
        assert retry.deadline == NOW + timedelta(seconds=600)
        Clock.current += timedelta(seconds=60)
        broker.bid, broker.ask = 3.0, 3.2
        broker.status_override = {"status": "FILLED", "quantity": 1, "filledQuantity": 1,
                                  "averagePrice": 3.1, "closedAt": Clock.current.isoformat()}
        entered = await supervisor.handle_signal(dep, signal(dep), dry_run=False)
        assert entered.order_id and len(broker.buy_orders) == 1
        assert entered.risk_details["entry_pricing"]["original_mid"] == pytest.approx(2.8)
        assert entered.risk_details["entry_pricing"]["starting_bid"] == 2.7
        assert entered.risk_details["entry_pricing"]["starting_ask"] == 2.9
        assert entered.risk_details["entry_pricing"]["max_entry_price"] == pytest.approx(3.22)
        assert entered.risk_details["entry_opportunity_deadline"] == (NOW + timedelta(seconds=600)).isoformat()
        assert entered.risk_details["entry_opportunity_id"] == original_id
        assert entered.risk_details["entry_retry_attempts"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("retries", [0, 600])
def test_shadow_temporary_reprice_obeys_retry_spacing_and_zero_disable(tmp_path, retries):
    async def run():
        broker = Broker()
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane(retries=retries)
        pending = await supervisor.handle_signal(dep, signal(dep), dry_run=True, simulate_only=True)
        assert pending.risk_details["paper_entry_status"] == "pending"
        original_expiry = supervisor._paper_entries[pending.trade_id][4]
        broker.preflight_failures = 1
        Clock.current += timedelta(seconds=31)
        await supervisor.poll_paper_entries()
        failed_count = len(broker.preflight_requests)
        assert "paper_reprice_attempt" not in pending.risk_details
        Clock.current += timedelta(seconds=1)
        await supervisor.poll_paper_entries()
        assert len(broker.preflight_requests) == failed_count
        Clock.current = NOW + timedelta(seconds=91)
        await supervisor.poll_paper_entries()
        active = supervisor._paper_entries[pending.trade_id][2]
        assert active.estimated_entry_price == (2.9 if retries else 2.8)
        assert supervisor._paper_entries[pending.trade_id][4] == original_expiry
        assert not broker.buy_orders and not planner.position_tracker.active_positions()
    asyncio.run(run())


def test_shadow_reprice_does_not_use_a_quote_aged_during_preflight(tmp_path):
    async def run():
        broker = Broker()
        planner, supervisor, _ = owners(tmp_path, broker)
        dep = lane()
        pending = await supervisor.handle_signal(dep, signal(dep), dry_run=True, simulate_only=True)
        original_preflight = broker.preflight_single_leg
        async def slow_preflight(payload):
            Clock.current += timedelta(seconds=9)
            return await original_preflight(payload)
        broker.preflight_single_leg = slow_preflight
        Clock.current = NOW + timedelta(seconds=31)
        await supervisor.poll_paper_entries()
        active = supervisor._paper_entries[pending.trade_id][2]
        assert active.estimated_entry_price == 2.8
        assert "paper_reprice_attempt" not in active.risk_details
        assert not broker.buy_orders
    asyncio.run(run())


def test_real_bootstrap_constructs_durable_entry_owner_before_buy(tmp_path, monkeypatch):
    from bhiksha.app.bootstrap import build_runtime
    from bhiksha.app import runtime as runtime_module
    config = tmp_path / "config"
    config.mkdir()
    (config / "strategy_catalog").mkdir()
    app = AppConfig(sqlite_path=str(tmp_path / "bootstrap.db"), strategy_catalog_dir="config/strategy_catalog",
                    playbook_artifacts_dir=str(tmp_path / "artifacts"), deployment_selection_mode="manual_only")
    (config / "app.yaml").write_text(yaml.safe_dump(app.model_dump()))
    (config / "providers.yaml").write_text(yaml.safe_dump({"underlying_live_primary": "public", "underlying_backfill_primary": "public"}))
    dep = lane()
    plan_path = tmp_path / "active_plan.json"
    plan_path.write_text(ActivePlan(active_plan_id="midpoint-bootstrap", deployments=[dep]).model_dump_json())
    runtime = build_runtime(config_root=config, active_plan_path=plan_path)
    broker = Broker()
    monkeypatch.setattr(runtime_module, "OrderManager", lambda **kwargs: OrderManager(broker=broker))
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_build_manual_status_writer", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_live_bar_source", lambda self: SimpleNamespace(close=AsyncMock()))
    captured = {}
    async def reconciliation(self, *, supervisor, **kwargs):
        captured["supervisor"] = supervisor
    async def warm(self, *args, **kwargs):
        supervisor = captured["supervisor"]
        planner = supervisor.planner
        assert planner.entry_intent_repository is supervisor.trade_state_repository
        planner.chain_service = StubChainService()
        broker.intent_repo = planner.entry_intent_repository
        captured["plan"] = await planner.plan_entry(dep, signal(dep), dry_run=False)
        return []
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_refresh_reconciliation", reconciliation)
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "warm_start_symbol", warm)
    asyncio.run(runtime.run_session(live=True, max_bars=0, output=lambda line: None))
    assert captured["plan"].order_id and len(broker.buy_orders) == 1
    assert captured["plan"].risk_details["entry_pricing"]["entry_policy_version"] == "midpoint_entry_v2"
