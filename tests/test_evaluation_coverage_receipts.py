"""Coverage proof follows runtime receipts, rather than hypothetical lost signals."""
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bhiksha.ops.trade_observation import summarize_evaluation_coverage
from bhiksha.ops.exit_comparisons_sheet import _signals
from bhiksha.app.runtime import _append_event_best_effort, record_signal_evaluation
from bhiksha.domain.models import SignalDecision
from bhiksha.domain.enums import SignalDirection

AT = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)
DEP = {'deployment_id': 'lane', 'symbol': 'SPY', 'enabled': True,
       'strategy': {'key': 'market_impulse'}}


def event(kind, payload, at=AT):
    return {'event_type': kind, 'payload': payload, 'created_at': at.isoformat()}


def interval(start=AT, end=None, strategy='market_impulse'):
    dep = {**DEP, 'strategy': {'key': strategy}}
    return [event('startup_config', {'config_fingerprint': 'same', 'deployments': [dep]}, start),
            event('evaluation_runtime_ready', {'config_fingerprint': 'same'}, start),
            event('evaluation_runtime_ended', {'config_fingerprint': 'same'}, end or start + timedelta(minutes=3))]


def tick(at=AT, source='closed_bar_1m'):
    return event('evaluation_source_tick', {'source': source, 'source_tick': at.isoformat(),
                  'symbol': 'SPY', 'deployment_ids': ['lane']}, at + timedelta(minutes=1))


def disposition(kind, at=AT, source='closed_bar_1m', **payload):
    return event(kind, {'deployment_id': 'lane', 'source': source, 'evaluation_source': source,
                       'source_tick': at.isoformat(), **payload}, at + timedelta(minutes=1))


def row(events):
    return summarize_evaluation_coverage(events)['lanes'][0]


def test_expected_observed_inactive_missing_reconcile_without_positive_doublecount():
    times = [AT + timedelta(minutes=i) for i in range(3)]
    events = interval() + [tick(t) for t in times] + [
        disposition('signal_evaluation', times[0], signal=True),
        disposition('evaluation_inactive', times[1], reason='existing_entry_or_position'),
        disposition('signal_decision', times[0], signal=True)]
    r = row(events)
    assert r['evaluation_count'] == 1
    assert (r['scheduled_evaluation_count'], r['observed_scheduled_evaluation_count'],
            r['intentional_inactive_count'], r['missing_evaluation_count']) == (3, 1, 1, 1)
    assert r['expected_source_tick_count'] == 3 and r['missing_source_tick_count'] == 0
    assert r['status'] == 'incomplete' and not r['expected_cadence_proved']
    events.append(disposition('signal_evaluation', times[2], signal=False))
    assert summarize_evaluation_coverage(events)['expected_cadence_proved'] is True
    assert row(events)['status'] == 'complete'


def test_missing_bar_is_unknown_opportunity_not_confirmed_missed_signal():
    events = interval() + [tick(), disposition('signal_evaluation', signal=False)]
    r = row(events)
    assert r['missing_source_tick_count'] == 2 and r['unknown_evaluation_count'] == 2
    assert r['missing_evaluation_count'] == 0 and r['evaluation_count'] == 1
    assert not r['expected_cadence_proved']


def test_repeated_fingerprint_does_not_erase_process_loss():
    first = interval()[:-1]
    later = AT + timedelta(minutes=10)
    events = first + interval(later, later + timedelta(minutes=1)) + [
        tick(later), disposition('signal_evaluation', later, signal=False)]
    report = summarize_evaluation_coverage(events)
    assert any(i['status'] == 'unknown_end' for i in report['runtime_intervals'])
    assert not report['expected_cadence_proved'] and row(events)['missing_source_tick_count'] is None


@pytest.mark.parametrize('start,end,count', [
    (datetime(2026,11,27,17,58,tzinfo=UTC), datetime(2026,11,27,18,5,tzinfo=UTC),2),
    (datetime(2026,10,9,13,31,30,tzinfo=UTC), datetime(2026,10,9,13,35,tzinfo=UTC),3),
    (datetime(2026,10,10,13,30,tzinfo=UTC), datetime(2026,10,10,14,30,tzinfo=UTC),0),
])
def test_calendar_early_close_late_start_holiday(start,end,count):
    r = row(interval(start,end))
    assert r['expected_source_tick_count'] == count
    assert r['missing_source_tick_count'] == count


def test_source_timestamp_normalization_and_manual_conditional_schedule():
    events = interval(end=AT + timedelta(minutes=1)) + [tick(), disposition('signal_evaluation', signal=False)]
    events[-1]['payload']['source_tick'] = AT.isoformat().replace('+00:00','Z')
    assert row(events)['expected_cadence_proved']
    manual = interval(end=AT + timedelta(minutes=1),strategy='manual_trigger') + [
        tick(), disposition('signal_evaluation', signal=False), tick(AT,'manual_intrabar'),
        disposition('signal_evaluation', AT,'manual_intrabar',signal=False)]
    r = row(manual)
    assert r['scheduled_evaluation_count'] == 2 and r['observed_scheduled_evaluation_count'] == 2
    assert r['expected_source_tick_count'] is None and not r['expected_cadence_proved']


def test_terminal_history_diagnostics_do_not_hide_waiting_arm_gap():
    events = [event('startup_config', {'deployments':[DEP]}),
        event('signal_evaluation', {'deployment_id':'lane', 'reason':['weekly_invalidated'],
                                   'features':{'evaluation_coverage':'incomplete'}}),
        event('signal_evaluation', {'deployment_id':'lane', 'reason':['weekly_confirmation_data_gap'],
                                   'features':{'evaluation_coverage':'incomplete'}})]
    r = row(events)
    assert r['incomplete_evaluation_count'] == 2
    assert r['terminal_diagnostic_count'] == 1 and r['blocking_incomplete_evaluation_count'] == 1
    assert r['schedule_status'] == 'unknown_runtime_interval'


@pytest.mark.asyncio
async def test_observational_recorder_failure_preserves_existing_evaluation():
    events=[]
    async def append(kind,payload):
        if kind.startswith('evaluation_'): raise sqlite3.OperationalError('database locked')
        events.append(event(kind,payload))
    repo=SimpleNamespace(append=append)
    output=[]
    await _append_event_best_effort(repo,'evaluation_source_tick',tick()['payload'],output=output.append)
    decision=SignalDecision('lane','SPY',AT,False,SignalDirection.LONG,['quiet'],{})
    await record_signal_evaluation(repo,decision,source='closed_bar_1m',source_tick=AT.isoformat())
    assert len(events)==1 and events[0]['event_type']=='signal_evaluation'
    assert row(interval()+events)['missing_source_tick_count']==3
    assert output and not row(interval()+events)['expected_cadence_proved']


def test_positive_handling_categories_and_aggregate_reconciliation(tmp_path):
    db=tmp_path/'events.db'
    outcomes=['filled','risk_block','execution_window_block','no_fill','pending_execution',None]
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,payload TEXT)')
        for i,outcome in enumerate(outcomes):
            p={'deployment_id':'lane','timestamp':(AT+timedelta(minutes=i)).isoformat(),'signal':True,'direction':'long'}
            for kind in ['signal_evaluation','signal_decision']:
                c.execute('INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)',(AT.isoformat(),kind,json.dumps(p)))
            if outcome:
                reasons={'risk_block':['risk_cap_blocked'],'execution_window_block':['execution_window_blocked'],
                         'no_fill':['paper_limit_expired']}.get(outcome,[])
                c.execute('INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)',
                          (AT.isoformat(),'signal_outcome',json.dumps({**p,'outcome':outcome,'rejection_reasons':reasons})))
        p={'deployment_id':'lane','timestamp':(AT+timedelta(minutes=6)).isoformat(),'signal':False,'reason':['quiet']}
        c.execute('INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)',(AT.isoformat(),'signal_evaluation',json.dumps(p)))
    r=_signals(db,AT.date(),{},now=AT+timedelta(minutes=7))
    assert r['recorded']==6 and r['evaluated']==r['decided']==6
    assert r['captured']==1 and r['missed']==3 and r['enqueued_pending']==1 and r['missing_outcome']==1
    assert r['recorded']==r['captured']+r['missed']+r['enqueued_pending']+r['missing_outcome']
    categories={x[5]:x[6] for x in r['opportunity_rows']}
    assert categories['Unknown']=='Unresolved / evidence needed'
    assert categories['Pending Execution']=='Enqueued; order state unproved'
    assert categories['No Fill']=='Valid limit unfilled'


@pytest.mark.parametrize('reason,expected', [
    ('paper_limit_expired','Valid limit unfilled'),
    ('entry_planning_error: timeout','Application failure'),
    ('entry_unfilled_closed REJECTED','Execution service failure'),
    ('preflight_failed','Execution service failure'),
    ('public_quote_stale_or_unproven','Market / quote provider'),
    ('unknown','Unfilled / reason unproved'),
])
def test_no_fill_requires_reason_evidence(reason,expected):
    from bhiksha.ops.exit_comparisons_sheet import _handling_category
    assert _handling_category({'outcome':'no_fill','reasons':[reason]},session_closed=True)==expected


def test_enqueue_is_not_current_working_order_proof():
    from bhiksha.ops.exit_comparisons_sheet import _handling_category
    row={'outcome':'pending_execution'}
    assert _handling_category(row,session_closed=False)=='Enqueued; order state unproved'
    assert _handling_category(row,session_closed=True)=='Unresolved enqueue at close'


@pytest.mark.asyncio
@pytest.mark.parametrize('drop_telemetry',[False,True])
async def test_production_bar_handler_keeps_false_evaluation_and_intentional_skip(monkeypatch,drop_telemetry):
    import asyncio
    from bhiksha.app.runtime import BhikshaRuntime, ReconciliationSnapshot
    from bhiksha.domain.models import Bar
    from bhiksha.domain.events import BarClosedEvent
    from bhiksha.market_data.bar_store import RollingBarStore
    runtime=SimpleNamespace(halt_and_flatten=False,risk_manager_flatten=False,
        _refresh_intraday_bias_controls=AsyncMock(return_value=False),
        _reconciliation_live_entry_block_reason=lambda _:None,
        _make_hard_flat_runner=lambda *a,**k:None,
        _instrument_execution_runner=lambda *a,**k:None,
        _repair_weekly_frame=AsyncMock(side_effect=lambda symbol,frame,*a:frame))
    events=[]
    async def append(kind,payload):
        if drop_telemetry and kind.startswith('evaluation_'):raise sqlite3.OperationalError('locked')
        events.append(event(kind,payload,AT+timedelta(minutes=1)))
    repo=SimpleNamespace(append=append)
    evaluated=SimpleNamespace(deployment_id='lane',symbol='SPY',strategy=SimpleNamespace(key='market_impulse'))
    inactive=SimpleNamespace(deployment_id='occupied',symbol='SPY',strategy=SimpleNamespace(key='market_impulse'))
    supervisor=SimpleNamespace(event_repository=repo,planner=SimpleNamespace(position_tracker=SimpleNamespace(active_positions=lambda:[])),
        due_exit_edge_continuation_options=lambda **k:[],can_submit_deployment_entry=lambda d:d is evaluated)
    evaluator=SimpleNamespace(prepare_enriched_frames=lambda *a:{},
        evaluate_entry=lambda d,f:SignalDecision('lane','SPY',AT,False,SignalDirection.LONG,['quiet'],{}))
    dispatcher=SimpleNamespace(submit=lambda *a,**k:False,queue_depth=lambda _:0,pending_count=lambda _:0)
    bar=Bar(symbol='SPY',timestamp=AT,open=100,high=100,low=100,close=100,volume=10)
    await BhikshaRuntime._handle_bar_event(runtime,BarClosedEvent(bar=bar,symbol='SPY',timeframe='1m',provider='schwab'),sync_lock=asyncio.Lock(),
        reconciliation_snapshot=ReconciliationSnapshot(),reconcile_trigger=asyncio.Event(),live=False,
        store=RollingBarStore(),supervisor=supervisor,position_monitor=SimpleNamespace(evaluate_symbol=lambda *a,**k:[]),
        evaluator=evaluator,execution_dispatcher=dispatcher,deployments_by_symbol={'SPY':[evaluated,inactive]},
        deployments_by_id={'lane':evaluated,'occupied':inactive},output=lambda _:None)
    evaluation=[e for e in events if e['event_type']=='signal_evaluation']
    assert len(evaluation)==1 and evaluation[0]['payload']['signal'] is False
    assert evaluation[0]['payload']['evaluation_source']=='closed_bar_1m'
    if not drop_telemetry:
        skips=[e['payload']for e in events if e['event_type']=='evaluation_inactive']
        assert skips==[{'deployment_id':'occupied','source':'closed_bar_1m','source_tick':AT.isoformat(),
                       'reason':'existing_entry_or_position'}]
