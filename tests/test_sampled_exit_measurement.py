from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import asyncio

import pytest

from bhiksha.ops import exit_edge_observer as observer
from bhiksha.ops.exit_edge_lab import (CURRENT_MEASUREMENT_GENERATION, ProspectiveQuoteTapeRepository,
    QuoteTapeMark, analyze_cases, classify_measurement_generation, experiment_spec_hash)
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder, _ObservedQuote
from bhiksha.execution.quote_lineage import quote_timestamp_evidence, MAX_ENTRY_QUOTE_AGE_SECONDS
from test_entry_exit_refactor import deployment

ENTRY = datetime(2026, 10, 9, 14, tzinfo=UTC)


def named_case(tmp_path, observations, *, legacy=False):
    recorder = ExitEdgeLiveRecorder(db_path=tmp_path/'edge.db', status_path=tmp_path/'status.json', role='registration')
    prepared = recorder.prepare_registration(deployment=deployment(), trade_id='sampled', option_symbol='QQQ261016C00500000',
        entry_timestamp=ENTRY, entry_premium=2, quantity=2)
    payload = prepared.payload
    assert payload is not None, prepared.attempt
    if legacy:
        payload['experiment']['max_freshness_ms'] = 5_000
        payload['experiment']['fill_model_version'] = 'next-fresh-natural-bid-v2'
        payload['experiment']['quote_feed'] = 'independent_public_option_observer_v1'
        payload['experiment'].pop('decision_clock')
        payload['experiment'].pop('poll_seconds')
        payload['experiment_spec_hash'] = experiment_spec_hash(payload['profile'], payload['legacy'], payload['experiment'])
    repo = ProspectiveQuoteTapeRepository(tmp_path/'edge.db');repo.initialize();repo.register_cohort(payload)
    for i,(seconds,age,bid) in enumerate(observations,1):
        received=ENTRY+timedelta(seconds=seconds)
        repo.append_quote(payload['cohort_id'],QuoteTapeMark(i,'public_api',payload['experiment']['quote_feed'],
            received-timedelta(seconds=age),received,bid,bid+.05))
    return repo.load_case(payload['cohort_id'])


def test_new_generation_freezes_receipt_clock_and_leaves_execution_quote_rule_strict(tmp_path):
    case=named_case(tmp_path,[(30,10,2)])
    assert case.experiment['max_freshness_ms']==30_000
    assert case.experiment['decision_clock']=='received_at'
    assert classify_measurement_generation(case.experiment)=='current'
    mark=case.quotes[0]
    quote=SimpleNamespace(quote_timestamp=mark.quote_at.isoformat(),quote_timestamp_field='quoteTimestamp')
    assert MAX_ENTRY_QUOTE_AGE_SECONDS == 8
    assert quote_timestamp_evidence(quote,mark.received_at)['quote_timestamp_status']=='stale'
    recorder=ExitEdgeLiveRecorder(db_path=tmp_path/'edge.db',status_path=tmp_path/'status.json',role='observer')
    observed=_ObservedQuote(case.option_symbol,mark.quote_at,'quoteTimestamp',mark.received_at,2,2.05,2.02)
    assert recorder._quote_rejection_reason(replace(case,quotes=()),observed) is None
    historical=replace(case,quotes=(),experiment={**case.experiment,'max_freshness_ms':5000})
    assert recorder._quote_rejection_reason(historical,observed)=='stale_quote_gap'
    too_old=replace(observed,received_at=mark.quote_at+timedelta(seconds=31))
    assert recorder._quote_rejection_reason(replace(case,quotes=()),too_old)=='stale_quote_gap'


def test_receipt_clock_does_not_backdate_timer_and_uses_later_bid(tmp_path):
    case=named_case(tmp_path,[(50,25,2),(65,25,2),(80,25,1.95)])
    row=analyze_cases([case],quote_sensitivity=True)['cases'][0]
    baseline=row['named_exit_outcomes']['baseline']
    assert baseline['exit_timestamp']==(ENTRY+timedelta(seconds=80)).isoformat()
    assert baseline['legs'][0]['trigger_sequence']==2
    assert baseline['legs'][0]['fill_sequence']==3
    assert baseline['realized_pnl_usd']==-10
    assert row['named_exit_outcomes']['patient'] is None
    assert row['quote_sensitivity']['named_exit_outcomes']=={}
    assert row['quote_sensitivity']['insufficient_reason']=='quote_tape_too_short_for_next_tick_fill'
    # A historical case still bases its timer on provider time.
    exp={**case.experiment,'decision_clock':'quote_at'}
    old=replace(case,experiment=exp,experiment_spec_hash=experiment_spec_hash(case.profile_config,case.legacy_config,exp))
    assert analyze_cases([old])['cases'][0]['named_exit_outcomes']['baseline'] is None


def test_strict_sensitivity_rebuilds_gaps_instead_of_claiming_filtered_tape_clean(tmp_path):
    case=named_case(tmp_path,[(30,1,2),(60,20,2),(75,20,2),(90,20,2),(105,1,2),(120,1,1.95)])
    row=analyze_cases([case],quote_sensitivity=True)['cases'][0]
    assert row['arm_evidence_status']['baseline']=='complete_clean'
    assert row['quote_sensitivity']['arm_evidence_status']['baseline']=='gap_affected_diagnostic'
    assert row['quote_sensitivity']['source_experiment_hash']==case.experiment_spec_hash
    assert case.experiment['max_freshness_ms']==30_000
    assert len(case.quotes)==6 and not case.observation_gaps


def test_historical_frozen_hash_and_quote_clock_remain_replayable(tmp_path):
    case=named_case(tmp_path,[(30,1,2),(61,0,2),(62,0,1.95)],legacy=True)
    row=analyze_cases([case],quote_sensitivity=True)['cases'][0]
    assert classify_measurement_generation(case.experiment)=='legacy'
    assert row['named_exit_outcomes']['baseline']['realized_pnl_usd']==-10
    assert row['quote_sensitivity']['named_exit_outcomes']==row['named_exit_outcomes']


@pytest.mark.asyncio
async def test_refresh_budget_is_one_rotating_batch_and_retains_first_observations(monkeypatch):
    monkeypatch.setattr(observer,'REFRESH_DELAY_SECONDS',.001)
    now=ENTRY+timedelta(seconds=100)
    def quote(age):
        return SimpleNamespace(quote_timestamp=(now-timedelta(seconds=age)).isoformat(),quote_timestamp_field='quoteTimestamp',bid=2,ask=2.05)
    class Reader:
        calls=[]
        async def quotes(self,symbols):
            self.calls.append(symbols)
            return {s:quote(45 if len(self.calls)%2 else 1) for s in symbols}
    class Recorder:
        observations=[];polls=[]
        def observe_quote(self,*args):self.observations.append(args)
        def record_observation_poll(self,*args,**kwargs):self.polls.append((args,kwargs))
        def record_observation_error(self,*args):raise AssertionError(args)
    reader=Reader();rec=Recorder();symbols=tuple(f'OPT{i:02}' for i in range(25))
    cursor=await observer.collect_observations(reader,rec,symbols,stop=asyncio.Event(),now_fn=lambda:now)
    assert len(reader.calls)==2 and len(reader.calls[1])==20
    assert len(rec.observations)==45 and rec.polls[-1][1]['refresh'] is True
    await observer.collect_observations(reader,rec,symbols,stop=asyncio.Event(),now_fn=lambda:now,refresh_cursor=cursor)
    assert reader.calls[3][0]=='OPT20'


@pytest.mark.asyncio
async def test_acceptable_sample_does_not_trigger_refresh(monkeypatch):
    from unittest.mock import AsyncMock,Mock
    now=ENTRY+timedelta(seconds=100)
    q=SimpleNamespace(quote_timestamp=(now-timedelta(seconds=20)).isoformat(),quote_timestamp_field='quoteTimestamp',bid=2,ask=2.05)
    reader=SimpleNamespace(quotes=AsyncMock(return_value={'OPT':q}));rec=Mock()
    await observer.collect_observations(reader,rec,('OPT',),stop=asyncio.Event(),now_fn=lambda:now)
    assert reader.quotes.await_count==1
    assert rec.observe_quote.call_count==1


@pytest.mark.asyncio
async def test_refresh_does_not_request_after_close(monkeypatch):
    from unittest.mock import AsyncMock,Mock
    monkeypatch.setattr(observer,'REFRESH_DELAY_SECONDS',.001)
    monkeypatch.setattr(observer,'_regular_session',lambda now:False)
    reader=SimpleNamespace(quotes=AsyncMock(return_value={}));rec=Mock()
    await observer.collect_observations(reader,rec,('OPT',),stop=asyncio.Event(),now_fn=lambda:ENTRY)
    assert reader.quotes.await_count==1


def test_sensitivity_does_not_resign_invalid_source_evidence(tmp_path):
    case=named_case(tmp_path,[(50,1,2),(65,1,2),(80,1,1.95)])
    tampered=replace(case,experiment_spec_hash="not-the-frozen-hash")
    row=analyze_cases([tampered],quote_sensitivity=True)['cases'][0]
    assert row['insufficient_reason']=='experiment_spec_hash_mismatch'
    assert row['quote_sensitivity']['named_exit_outcomes']=={}
    assert row['quote_sensitivity']['insufficient_reason']=='experiment_spec_hash_mismatch'


@pytest.mark.asyncio
async def test_response_crossing_session_close_is_not_a_modeled_fill():
    from unittest.mock import AsyncMock,Mock
    closed=datetime(2026,10,9,20,0,1,tzinfo=UTC)
    q=SimpleNamespace(quote_timestamp=closed.isoformat(),quote_timestamp_field='quoteTimestamp',bid=2,ask=2.05)
    reader=SimpleNamespace(quotes=AsyncMock(return_value={'OPT':q}));rec=Mock()
    await observer.collect_observations(reader,rec,('OPT',),stop=asyncio.Event(),now_fn=lambda:closed)
    assert rec.observe_quote.call_count==0
