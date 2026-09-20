from datetime import UTC, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
import asyncio

import pytest

from bhiksha.config.models import AppConfig
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan, OptionSelectionRequest, OptionContractSnapshot
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector
from bhiksha.state.position_tracker import PositionTracker
from test_cartographer_profile_compiler import _row, _compile, _operator_defaults


class Clock(datetime):
    current = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def setup(tmp_path, monkeypatch):
    Clock.current = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
    monkeypatch.setattr('bhiksha.execution.supervisor.datetime', Clock)
    defaults = _operator_defaults()
    defaults['profile__trend_continuation'].update(entry_liquidity_retry_seconds=600,
        entry_liquidity_retry_interval_seconds=60)
    deployment = _compile(tmp_path, _row(defaults), defaults).plan.deployments[0]
    deployment.source.metadata.update(row_index=2, sheet_name="manual_entry")
    events = []
    class Planner:
        calls = 0
        position_tracker = PositionTracker()
        error = SelectorEmptyError(deployment.deployment_id, {'spread_above_max': 1},
            {'liquidity_retry_candidates': 1})
        async def plan_entry(self, *args, **kwargs):
            self.calls += 1
            if self.error:
                raise self.error
            return TradePlan(trade_id='budget', deployment_id=deployment.deployment_id,
                symbol='SPY', direction=SignalDirection.LONG, option_symbol='SPY_OPTION',
                quantity=0, estimated_entry_price=20, risk_reasons=['insufficient_budget'], dry_run=True)
    class Events:
        async def append(self, kind, payload):
            events.append((kind, payload))
    planner = Planner()
    supervisor = ExecutionSupervisor(planner=planner, event_repository=Events(), app_config=AppConfig())
    def decision(**kwargs):
        return SignalDecision(deployment_id=deployment.deployment_id, symbol='SPY',
            timestamp=kwargs.get('timestamp', Clock.current), signal=kwargs.get('signal', True),
            direction=SignalDirection.LONG, reason=['manual_trigger_met'], features={'close':kwargs.get('price',601)})
    return supervisor, planner, deployment, decision, events


def test_retry_is_bounded_spaced_and_budget_is_terminal(setup):
    s, p, d, decision, events = setup
    async def run():
        assert await s.handle_signal(d, decision(), dry_run=True, simulate_only=True) is None
        deadline = s._entry_liquidity_retries[d.deployment_id].deadline
        assert not s.can_submit_deployment_entry(d)
        Clock.current += timedelta(seconds=59)
        await s.handle_signal(d, decision(), dry_run=True, simulate_only=True)
        assert p.calls == 1
        Clock.current += timedelta(seconds=1)
        assert s.can_submit_deployment_entry(d)
        await s.handle_signal(d, decision(timestamp=Clock.current-timedelta(seconds=6)), dry_run=True, simulate_only=True)
        await s.handle_signal(d, decision(signal=False), dry_run=True, simulate_only=True)
        assert p.calls == 1
        await s.handle_signal(d, decision(), dry_run=True, simulate_only=True)
        assert p.calls == 2
        assert s._entry_liquidity_retries[d.deployment_id].deadline == deadline
        Clock.current += timedelta(seconds=60)
        p.error = None
        plan = await s.handle_signal(d, decision(), dry_run=True, simulate_only=True)
        assert plan.quantity == 0
        assert not s.has_entry_liquidity_retry(d.deployment_id)
        assert not s.can_submit_deployment_entry(d)
        await s.handle_signal(d, decision(), dry_run=True, simulate_only=True)
        assert p.calls == 3
    asyncio.run(run())
    assert sum(k == 'entry_liquidity_retry_scheduled' for k, _ in events) == 2


@pytest.mark.parametrize('stop', ['expiry', 'invalidation', 'window', 'validity'])
def test_retry_cancels_without_rearming(setup, stop):
    s,p,d,decision,events = setup
    async def run():
        await s.handle_signal(d, decision(), dry_run=True, simulate_only=True)
        if stop == 'invalidation':
            await s.observe_entry_liquidity_retry(d, price=589, timestamp=Clock.current)
        else:
            if stop == 'expiry': Clock.current += timedelta(minutes=10)
            if stop == 'window': s._entry_liquidity_retries[d.deployment_id].deployment.execution.entry_window_end_et='09:59'
            if stop == 'validity': s._entry_liquidity_retries[d.deployment_id].deployment.source.metadata['valid_through']=Clock.current.isoformat()
            await s.manage_pending_exits({d.deployment_id:d}, now=Clock.current)
        assert not s.has_entry_liquidity_retry(d.deployment_id)
        assert not s.can_submit_deployment_entry(d)
        assert p.calls == 1
    asyncio.run(run())
    assert any(k=='entry_liquidity_retry_finished' for k,_ in events)


@pytest.mark.parametrize('kind',['oi','budget_exception','off','other_owner'])
def test_no_retry_for_non_liquidity_failures_or_unarmed_lanes(setup,kind):
    s,p,d,decision,_ = setup
    if kind=='oi': p.error=SelectorEmptyError(d.deployment_id, {'open_interest_below_min':1}, {'liquidity_retry_candidates':0})
    if kind=='budget_exception': p.error=ValueError('insufficient_budget')
    if kind=='off': d.execution.entry_liquidity_retry_seconds=0
    if kind=='other_owner': d.source.metadata['source_owner']='operator'
    async def run():
        with pytest.raises((SelectorEmptyError,ValueError)):
            await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        assert not s.has_entry_liquidity_retry(d.deployment_id)
    asyncio.run(run())


def test_selector_spread_evidence_includes_bounded_later_expiry():
    request=OptionSelectionRequest(deployment_id='x',symbol='SPY',direction=SignalDirection.LONG,
        signal_timestamp=Clock.current,execution_profile='single_leg_long_premium_v1',execution_params={
            'dte_min':3,'dte_max':7,'dte_fallback_policy':'allow_nearest_after','dte_fallback_max':21,
            'min_open_interest':50,'target_abs_delta_min':.15,'target_abs_delta_max':.35,
            'max_bid_ask_spread_pct':.2})
    contract=OptionContractSnapshot(option_symbol='x',underlying_symbol='SPY',contract_type='CALL',
        expiration_date='2026-08-28',dte=11,strike=600,delta=.3,bid=1,ask=2,open_interest=100)
    selector=SingleLegOptionSelector()
    with pytest.raises(SelectorEmptyError) as exc:
        selector.select(request,[contract])
    assert exc.value.diagnostics['liquidity_retry_candidates']==1
    with pytest.raises(SelectorEmptyError) as exc:
        selector.select(request,[replace(contract,open_interest=0)])
    assert exc.value.diagnostics['liquidity_retry_candidates']==0
    assert selector.select(request,[replace(contract,bid=1.9)]).dte==11


def test_retry_uses_fresh_trigger_then_stops_after_selection(setup, monkeypatch):
    from bhiksha.strategy.manual_trigger import ManualTriggerStrategy
    import polars as pl
    s,p,d,decision,_ = setup
    monkeypatch.setattr('bhiksha.execution.cartographer_invalidation.datetime',Clock)
    async def run():
        await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        Clock.current += timedelta(seconds=60)
        frame=pl.DataFrame({'symbol':['SPY','SPY'],'timestamp':[Clock.current-timedelta(minutes=1),Clock.current], 'close':[601.,602.]})
        strategy=ManualTriggerStrategy()
        # A historical first-trigger latch must not silently swallow a retry.
        assert not strategy.evaluate_entry(frame,d.deployment_id,d.strategy.params).signal
        current=strategy.evaluate_entry(frame.tail(1),d.deployment_id,d.strategy.params)
        assert current.signal
        waiting=strategy.evaluate_entry(frame.tail(1).with_columns(pl.lit(599.).alias('close')),d.deployment_id,d.strategy.params)
        assert not waiting.signal
        async def approved(*args,**kwargs):
            p.calls += 1
            return TradePlan(trade_id='selected',deployment_id=d.deployment_id,symbol='SPY',
                direction=SignalDirection.LONG,option_symbol='SPY_OPTION',quantity=1,
                estimated_entry_price=2,risk_reasons=['approved'],dry_run=True,entry_timestamp=Clock.current)
        p.plan_entry=approved
        plan=await s.handle_signal(d,current,dry_run=True,simulate_only=True)
        assert plan.quantity==1
        assert not s.has_entry_liquidity_retry(d.deployment_id)
        assert not s.can_submit_deployment_entry(d)
        assert p.calls==2
    asyncio.run(run())


def test_live_infrastructure_block_is_not_retried(setup):
    s,p,d,decision,_=setup
    async def run():
        await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        Clock.current += timedelta(seconds=60)
        plan=await s.handle_signal(d,decision(),dry_run=False,simulate_only=False,
            live_entry_block_reason='reconciliation_too_stale')
        assert plan.risk_reasons==['reconciliation_too_stale']
        assert p.calls==1
        assert not s.has_entry_liquidity_retry(d.deployment_id)
    asyncio.run(run())


def test_restart_from_stale_plan_cannot_rearm_consumed_retry(setup, monkeypatch):
    from bhiksha.active_plan.runtime import reconcile_cartographer_attempts
    s,p,d,decision,events=setup
    async def run():
        await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        ledger=[{'event_type':kind,'payload':payload} for kind,payload in events]
        monkeypatch.setattr('bhiksha.active_plan.runtime.load_attempt_events',lambda path:ledger)
        restarted=ExecutionSupervisor(planner=p)
        assert restarted.can_submit_deployment_entry(d)
        result=await reconcile_cartographer_attempts(events_db_path='unused',event_repository=restarted.event_repository,
            supervisor=restarted,deployments_by_id={d.deployment_id:d},trade_state_repository=None,
            live=True,now=Clock.current,output=lambda line:None)
        assert result['replayed']==0
        assert not restarted.has_entry_liquidity_retry(d.deployment_id)
        assert not restarted.can_submit_deployment_entry(d)
        assert p.calls==1
    asyncio.run(run())
