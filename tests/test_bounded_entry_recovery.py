"""Entry recovery keeps price, eligibility, reservations and order identity intact."""
import asyncio
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from bhiksha.execution.order_manager import OrderManager, PublicQuote, snap_price
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.domain.models import SignalDecision
from bhiksha.domain.enums import SignalDirection
from bhiksha.state.position_tracker import PositionTracker
from test_execution_planner import (StubChainService, StubOrderManager, StubRiskManager,
    RecordingAllowedCashGuard, _enabled_deployment)
from test_session_entry_repairs import freeze, Clock, NOW, quote
from test_entry_liquidity_retry import setup


def test_tick_correction_preflights_exact_cost_and_never_rounds_buy_up():
    class Broker:
        prices=[]
        async def preflight_single_leg(self,payload):
            self.prices.append(payload['limitPrice'])
            return {'priceIncrement':{'currentIncrement':'.05'},
                    'estimatedCost':float(payload['limitPrice'])*100+.02}
    broker=Broker()
    result=asyncio.run(OrderManager(broker=broker).preflight_entry('SPY260410C00600000',3.64,1))
    assert broker.prices == ['3.64','3.60']
    assert result.payload['limitPrice']=='3.60'
    assert result.estimated_cost==360.02
    assert snap_price(3.65,.05,side='BUY')==3.65
    with pytest.raises(ValueError,match='price_below_broker_increment'):
        snap_price(.04,.05,side='BUY')


@pytest.mark.parametrize('failure',[TimeoutError('increments of $0.05'), None])
def test_ambiguous_buy_keeps_client_identity_and_never_resubmits(failure):
    class Broker:
        submissions=0
        async def preflight_single_leg(self,payload): return {}
        async def place_order(self,payload):
            self.submissions+=1
            if failure: raise failure
            return {}
    broker=Broker()
    result=asyncio.run(OrderManager(broker=broker).place_entry_order('SPY260410C00600000',3.64,1,order_id='durable-id'))
    assert broker.submissions==1
    assert result.order_id=='durable-id' and result.submission_uncertain
    assert not result.increment_rejected


def test_affordable_alternative_stays_in_original_dte_cohort(monkeypatch):
    freeze(monkeypatch)
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    dep.risk.max_trade_premium_usd=400
    dep.execution.dte_min=0; dep.execution.dte_max=0
    dep.execution.dte_fallback_policy='allow_nearest_after'; dep.execution.dte_fallback_max=7
    class Chain(StubChainService):
        async def get_chain(self,*args,**kwargs):
            one=replace((await super().get_chain(*args,**kwargs))[0],delta=-.30)
            return [one,replace(one,option_symbol='QQQ260330P00557000',delta=-.31),
                    replace(one,option_symbol='QQQ260406P00557000',dte=7,delta=-.31)]
    class Manager(StubOrderManager):
        symbols=[]
        async def get_option_quote(self,symbol):
            self.symbols.append(symbol)
            expensive=symbol.endswith('00558000')
            return PublicQuote(symbol,bid=8 if expensive else 2.7,ask=9 if expensive else 2.9,
                open_interest=550,quote_timestamp=Clock.current.isoformat(),quote_timestamp_field='quoteTimestamp')
    chain=Chain(); manager=Manager()
    planner=ExecutionPlanner(chain_service=chain,order_manager=manager,position_tracker=PositionTracker())
    decision=SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{})
    plan=asyncio.run(planner.plan_entry(dep,decision,dry_run=True,simulate_only=True))
    assert plan.option_symbol=='QQQ260330P00557000' and plan.quantity==1
    assert chain.calls==1 and manager.symbols==['QQQ260330P00558000','QQQ260330P00557000']
    assert len(plan.risk_details['entry_recovery_attempts'])==2
    # Removing the affordable primary contract must not activate farther DTE.
    dep.risk.max_trade_premium_usd=100
    manager.symbols=[]
    plan=asyncio.run(planner.plan_entry(dep,decision,dry_run=True,simulate_only=True))
    assert plan.quantity==0 and all('260406' not in s for s in manager.symbols)


def test_final_age_rebuild_releases_old_reservations_then_submits_once(monkeypatch):
    freeze(monkeypatch)
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode='price_seeking'
    manager=StubOrderManager()
    manager.get_option_quote=AsyncMock(side_effect=lambda _: quote(Clock.current))
    original=manager.preflight_entry
    async def preflight(symbol,limit,quantity):
        result=await original(symbol,limit,quantity); result.payload['limitPrice']=str(limit); return result
    manager.preflight_entry=preflight
    risk=StubRiskManager(); cash=RecordingAllowedCashGuard(); reserve=risk.reserve_sized_entry
    async def delayed(**kwargs):
        result=await reserve(**kwargs)
        if len(risk.reserve_calls)==1: Clock.current+=timedelta(seconds=9)
        return result
    risk.reserve_sized_entry=delayed
    planner=ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,risk_manager=risk,
        cash_guard=cash,position_tracker=PositionTracker())
    plan=asyncio.run(planner.plan_entry(dep,SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{}),dry_run=False))
    assert plan.order_id and manager.place_entry_order_calls==1
    assert plan.risk_details['final_quote_rebuild_used']
    assert len(risk.reserve_calls)==2 and len(risk.release_calls)==1
    assert ('release',risk.release_calls[0]) in cash.calls
    assert risk.release_calls[0]!=plan.trade_id


def test_operator_manual_recovery_keeps_deadline_and_restart_consumption(setup):
    s,p,d,decision,events=setup
    d.source.metadata['source_owner']='operator'
    async def run():
        await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        first=s._entry_liquidity_retries[d.deployment_id].deadline
        from test_entry_liquidity_retry import Clock as RetryClock
        RetryClock.current+=timedelta(seconds=60)
        await s.handle_signal(d,decision(),dry_run=True,simulate_only=True)
        assert s._entry_liquidity_retries[d.deployment_id].deadline==first
        from bhiksha.execution.entry_retry import restore_consumed_retry_intents
        from bhiksha.execution.supervisor import ExecutionSupervisor
        restarted=ExecutionSupervisor(planner=p)
        restored=restore_consumed_retry_intents([{'event_type':k,'payload':v} for k,v in events],{d.deployment_id:d},restarted)
        assert restored=={d.deployment_id} and not restarted.can_submit_deployment_entry(d)
    asyncio.run(run())


def test_uncertain_submission_keeps_risk_and_cash_hold(monkeypatch):
    freeze(monkeypatch)
    from bhiksha.execution.order_manager import OrderResult
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    manager=StubOrderManager()
    manager.place_entry_order=AsyncMock(return_value=OrderResult(order_id='durable-id',
        error='timeout',submission_uncertain=True,actual_limit_price=2.9))
    cash=RecordingAllowedCashGuard(); risk=StubRiskManager(); tracker=PositionTracker()
    planner=ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,
        risk_manager=risk,cash_guard=cash,position_tracker=tracker)
    plan=asyncio.run(planner.plan_entry(dep,SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{}),dry_run=False))
    assert plan.order_id=='durable-id' and manager.place_entry_order.await_count==1
    assert not risk.release_calls and not any(action=='release' for action,_ in cash.calls)
    assert plan.risk_details['entry_pricing']['submission_uncertain']


def test_recovery_expiry_stops_final_refresh_before_submission(monkeypatch):
    freeze(monkeypatch)
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    manager=StubOrderManager(); risk=StubRiskManager(); reserve=risk.reserve_sized_entry
    async def delayed(**kwargs):
        result=await reserve(**kwargs); Clock.current+=timedelta(seconds=9); return result
    risk.reserve_sized_entry=delayed
    planner=ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,risk_manager=risk,
        cash_guard=RecordingAllowedCashGuard(),position_tracker=PositionTracker())
    plan=asyncio.run(planner.plan_entry(dep,SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{}),dry_run=False,
        entry_guard=lambda:'entry_window_closed' if Clock.current>NOW else None))
    assert 'entry_window_closed' in plan.risk_reasons
    assert manager.place_entry_order_calls==0 and risk.release_calls


def test_report_groups_attempts_into_one_opportunity(tmp_path):
    import sqlite3,json
    from bhiksha.ops.exit_comparisons_sheet import _signals
    db=tmp_path/'events.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,payload TEXT)')
        for i in range(5):
            stamp=f'2026-09-28T14:0{i}:00+00:00'
            payload={'deployment_id':'PANW-shadow','symbol':'PANW','timestamp':stamp,
                'direction':'long','mode':'shadow','opportunity_id':'one-opportunity',
                'outcome':'pending_execution' if i<4 else 'no_fill',
                'rejection_reasons':[] if i<4 else ['paper_entry_expired']}
            con.execute('INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)',
                (stamp,'signal_outcome',json.dumps(payload)))
    result=_signals(db,NOW.date(),{})
    assert result['recorded']==5 and result['opportunities']==1
    assert result['opportunity_rows'][0][4:7]==[5,'No Fill','Valid limit unfilled']


@pytest.mark.parametrize('failure,retryable',[(TimeoutError('temporary'),True),(ValueError('bad configuration'),False)])
def test_preflight_recovery_distinguishes_transient_from_configuration(monkeypatch,failure,retryable):
    freeze(monkeypatch)
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    manager=StubOrderManager(); manager.preflight_entry=AsyncMock(side_effect=failure)
    planner=ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,
        risk_manager=StubRiskManager(),cash_guard=RecordingAllowedCashGuard(),position_tracker=PositionTracker())
    plan=asyncio.run(planner.plan_entry(dep,SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{}),dry_run=False))
    assert plan.risk_details['pre_submission_retryable'] is retryable
    assert manager.place_entry_order_calls==0


def test_strategy_retry_uses_completed_bar_not_manual_observation_age(setup):
    s,p,d,decision,events=setup
    d.strategy.key='market_impulse'
    d.source.metadata={}
    d.execution.shadow_only=True
    async def run():
        from test_entry_liquidity_retry import Clock as RetryClock
        from bhiksha.execution.entry_retry import EntryLiquidityRetry
        original=decision()
        retry=EntryLiquidityRetry(d,RetryClock.current+timedelta(seconds=600),RetryClock.current,
            pending_decision=original,opportunity_id='original')
        s._entry_liquidity_retries[d.deployment_id]=retry
        RetryClock.current+=timedelta(seconds=60)
        # The completed minute's timestamp is older than five seconds, unlike
        # a manual quote tick. Its strategy qualification remains authoritative.
        bar_decision=decision(timestamp=RetryClock.current-timedelta(seconds=60))
        await s.handle_signal(d,bar_decision,dry_run=True,simulate_only=True)
        assert p.calls==1 and s.has_entry_liquidity_retry(d.deployment_id)
    asyncio.run(run())


def test_operator_execution_alias_preserves_entry_window():
    from bhiksha.active_plan.compiler import ActivePlanSheetRow
    row=ActivePlanSheetRow.model_validate({'row_id':'existing','row_type':'strategy','enabled':True,'strategy_id':'existing',
        'execution_overrides':'{"entry_window_end_et":"10:14"}',
        'execution':'{"entry_liquidity_retry_seconds":600,"min_open_interest":50}'})
    assert row.execution_overrides == {'entry_window_end_et':'10:14',
        'entry_liquidity_retry_seconds':600,'min_open_interest':50}
