from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import polars as pl
import pytest

from bhiksha.integrations.cartographer_weekly import DEFAULTS, atomic_json, controls, resolve_condition
from bhiksha.strategy.weekly_chart import completed, confirmations, observe, reserve, state, transition, _RECOVERED


def frame(day='2026-09-28', minutes=39, price=600.5):
    opening=datetime.fromisoformat(day+'T13:30:00+00:00')
    return pl.DataFrame({'timestamp':[opening+timedelta(minutes=i) for i in range(minutes)], 'close':[price]*minutes,'symbol':['SPY']*minutes})


@pytest.fixture
def params(tmp_path):
    health=tmp_path/'health.json'
    atomic_json(health,{'ok':True,'checked_at':'2026-09-28T12:30:00+00:00'})
    return {'deployment_id':'cw-example-bull','scenario_key':'cw-example','symbol':'SPY','direction':'long',
        'publication_hash':'sha256:test','published_at':'2026-09-27T11:00:00+00:00','admitted_at':'2026-09-27T12:00:00+00:00',
        'valid_through':'2026-10-02T20:00:00+00:00','controls':deepcopy(DEFAULTS),
        'source_health_path':str(health),'state_db':str(tmp_path/'bhiksha.db'),
        'trigger':{'rule':'close_above','timeframe':'39m','count':1,'price':600},
        'tactical_invalidation':{'rule':'close_below','timeframe':'39m','count':1,'price':590}}


def deployment(p):
    d=SimpleNamespace(deployment_id=p['deployment_id'],strategy=SimpleNamespace(key='weekly_chart',params=p),execution=SimpleNamespace(shadow_only=True))
    d.model_dump_json=lambda:json.dumps({'strategy':{'params':p}})
    return d


def test_no_entry_until_39_minute_bar_closes(params):
    now=datetime(2026,9,28,14,8,59,tzinfo=UTC)
    assert observe(frame(minutes=38),params,now)[0]['reason']=='weekly_waiting_confirmation'
    now+=timedelta(seconds=1)
    assert observe(frame(),params,now)[0]['reason']=='weekly_confirmed'


def test_missing_minute_and_prepublication_cannot_confirm(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    assert not confirmations(frame().slice(1),params['trigger'],now,datetime(2026,9,27,tzinfo=UTC))
    params['admitted_at']='2026-09-28T13:31:00+00:00'
    assert observe(frame(),params,now)[0]['reason']=='weekly_waiting_confirmation'


def test_daily_only_next_session_with_fresh_price(params):
    params['trigger']['timeframe']='daily'
    params['tactical_invalidation']['timeframe']='daily'
    now=datetime(2026,9,28,20,tzinfo=UTC)
    assert observe(frame(minutes=390),params,now)[0]['reason']=='weekly_waiting_next_session_confirmation'
    atomic_json(__import__('pathlib').Path(params['source_health_path']),{'ok':True,'checked_at':'2026-09-29T12:30:00+00:00'})
    now=datetime(2026,9,29,13,35,tzinfo=UTC)
    bars=pl.concat([frame(minutes=390),frame('2026-09-29',minutes=5)])
    assert observe(bars,params,now)[0]['reason']=='weekly_confirmed'
    assert observe(bars,params,now+timedelta(minutes=3))[0]['reason']=='weekly_underlying_stale'


def test_invalidation_wins_and_persists(params):
    params['tactical_invalidation']['price']=601
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    data,status=observe(frame(),params,now)
    assert status=='invalidated'
    params['tactical_invalidation']['price']=590
    assert observe(frame(price=600.9),params,now)[1]=='invalidated'


def test_scenario_reservation_fill_and_restart(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    observe(frame(),params,now)
    d=deployment(params)
    assert reserve(d,now,0) is None
    other=deepcopy(params);other['deployment_id']='cw-example-bear'
    observe(frame(),other,now)
    assert reserve(deployment(other),now,0)=='weekly_scenario_already_reserved_or_consumed'
    transition(d,'filled','trade')
    transition(d,'waiting')
    _RECOVERED.clear()
    assert observe(frame(),params,now)[1]=='filled'
    assert reserve(deployment(other),now,0)=='weekly_scenario_already_reserved_or_consumed'


def test_paper_pending_restart_without_fill_can_retry(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    observe(frame(),params,now);d=deployment(params)
    assert reserve(d,now,0) is None
    _RECOVERED.clear()
    data,status=observe(frame(),params,now)
    assert status=='waiting'
    assert data['recovery']=='paper_pending_cancelled_on_restart'


def test_source_failure_distance_and_retry_bound(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    assert observe(frame(price=620),params,now)[0]['reason']=='weekly_entry_distance_exceeded'
    assert observe(frame(minutes=50),params,now+timedelta(minutes=11))[0]['reason']=='weekly_retry_window_expired'
    params['controls']['mode']='OFF'
    assert observe(frame(),params,now)[0]['reason']=='weekly_operator_off'


def test_early_close_daily_and_consecutive_hole(params):
    opening=datetime(2026,11,27,14,30,tzinfo=UTC)
    bars=pl.DataFrame({'timestamp':[opening+timedelta(minutes=i) for i in range(210)],'close':[601.0]*210,'symbol':['SPY']*210})
    condition={**params['trigger'],'timeframe':'daily'}
    assert len(completed(bars,condition,opening+timedelta(minutes=210)))==1
    assert not completed(bars,condition,opening+timedelta(minutes=209))
    condition={**params['trigger'],'count':2}
    bars=frame(minutes=78).filter(pl.col('timestamp')!=datetime(2026,9,28,13,35,tzinfo=UTC))
    assert not confirmations(bars,condition,datetime(2026,9,28,14,48,tzinfo=UTC),datetime(2026,9,27,tzinfo=UTC))


def test_controls_refuse_live_and_wrong_bounds():
    with pytest.raises(ValueError): controls({'cartographer_weekly':{'mode':'LIVE'}})
    with pytest.raises(ValueError): controls({'cartographer_weekly':{'dte_min':30}})
    assert controls({})['mode']=='OFF'


def test_condition_does_not_guess_unsupported_rules():
    with pytest.raises(ValueError): resolve_condition({'rule':'retest'}, {})


def test_invalidated_pending_keeps_sibling_reserved_until_cancel(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    observe(frame(),params,now);d=deployment(params)
    assert reserve(d,now,0) is None
    params['tactical_invalidation']['price']=601
    data,status=observe(frame(),params,now)
    assert status=='pending' and data['reason']=='weekly_invalidated'
    sibling=deepcopy(params);sibling['deployment_id']='cw-example-other';sibling['tactical_invalidation']['price']=590
    observe(frame(),sibling,now)
    assert reserve(deployment(sibling),now,0)=='weekly_scenario_already_reserved_or_consumed'
    transition(d,'waiting')
    assert observe(frame(),params,now)[1]=='invalidated'
    assert reserve(deployment(sibling),now,0) is None


def test_compiler_preserves_weekly_identity_and_named_exits(tmp_path, params):
    from bhiksha.integrations.cartographer_weekly import build_rows
    from bhiksha.active_plan.compiler import compile_active_plan_from_rows
    from bhiksha.config.exit_catalog import ExitProfileConfig
    root=tmp_path/'admissions';root.mkdir()
    p={**params,'author_profile':'TREND_CONTINUATION','setup_type':'upside_breakout','early_admitted_at':params['admitted_at']}
    atomic_json(root/'admissions.json',{'plans':[p]})
    control={**DEFAULTS,'compare_exits':'trend_continuation_balanced'}
    rows=build_rows({'cartographer_weekly':control},root,str(tmp_path/'db'))
    name='trend_continuation_balanced'
    catalog={name:ExitProfileConfig(exit_profile_id=name,trade_archetype='TREND_CONTINUATION',exit_family='staged_r_ladder',target_1_r=1,target_2_r=2,target_1_quantity=.6,initial_stop_pct=.3,disaster_stop_pct=.35,no_progress_seconds=2700,giveback_policy='OFF',breakeven_after_t1=True,eod_flat=True,hard_flat_time_et='15:55')}
    deployments=compile_active_plan_from_rows(rows=rows,strategy_catalog_path=tmp_path/'catalog',exit_profiles_catalog=catalog).plan.deployments
    d,early=deployments
    assert early.exit==d.exit and early.risk==d.risk and early.execution==d.execution
    assert early.strategy.params['entry_arm']=='early_1m'
    assert early.source.metadata['strategy_class'].endswith('__early_1m')
    assert d.strategy.key=='weekly_chart' and d.execution.shadow_only
    assert d.source.origin=='cartographer_weekly' and d.exit.use_algorithmic_exit
    assert d.risk.max_trade_premium_usd==400 and d.execution.min_open_interest==0
    assert d.execution.preferred_min_open_interest==100 and d.execution.entry_execution_profile=='balanced'
    assert d.execution.entry_pricing_require_open_interest
    assert d.exit.management_exit==name and d.exit.compare_exits==[name]


def test_receipt_hashes_and_revision_block(tmp_path):
    import hashlib
    from bhiksha.integrations.cartographer_weekly import import_publication
    root=tmp_path/'source';run=root/'runs'/'one';run.mkdir(parents=True)
    ident={'pack_id':'pack-one','observation_time':'2026-09-25T20:00:00+00:00','information_cutoff':'2026-09-26T00:00:00+00:00'}
    def rule(anchor,kind): return {'anchor_id':anchor,'rule':kind,'confirmation_timeframe':'39m','minimum_completed_bars':1}
    branch={'id':'bull','bias':'bull','expires_at':'2026-10-02T20:00:00+00:00','trigger':rule('high','close_above'),'tactical_invalidation':rule('low','close_below'),'structural_invalidation':rule('low','close_below')}
    book={**ident,'schema':'market_cartographer.weekly_market_book.v1','scenarios':[{'scenario_id':'one','symbol':'SPY','disposition':'scenario','management_profile':'TREND_CONTINUATION','setup_type':'breakout','what_if':{'branches':[branch]}}]}
    packet={**ident,'candidates':[{'symbol':'SPY','what_if_anchors':[{'anchor_id':'high','price':600},{'anchor_id':'low','price':590}]}]}
    docs={'weekly-book.json':book,'analyst-packet.json':packet,'evidence.json':{'data_mode':'market'}}
    def publish():
        items=[]
        for name,value in docs.items():
            text=json.dumps(value);(run/name).write_text(text)
            items.append({'path':name,'sha256':'sha256:'+hashlib.sha256(text.encode()).hexdigest()})
        atomic_json(run/'receipt.json',{**ident,'status':'succeeded','run_id':'one','artifacts':items})
        atomic_json(root/'latest.json',{'run_dir':str(run),'updated_at':'2026-09-27T10:00:00+00:00'})
    publish();target=tmp_path/'admitted';first=datetime(2026,9,27,12,tzinfo=UTC)
    a=import_publication(root/'latest.json',target,first)
    b=import_publication(root/'latest.json',target,first+timedelta(hours=1))
    assert a['plans'][0]['admitted_at']==b['plans'][0]['admitted_at']
    paired=import_publication(root/'latest.json',target,first+timedelta(minutes=90),policy={**DEFAULTS,'entry_timing_comparison':'PAIRED'})
    again=import_publication(root/'latest.json',target,first+timedelta(minutes=100),policy={**DEFAULTS,'entry_timing_comparison':'PAIRED'})
    assert paired['plans'][0]['early_admitted_at']==again['plans'][0]['early_admitted_at']
    assert paired['plans'][0]['admitted_at']==a['plans'][0]['admitted_at']
    (run/'weekly-book.json').write_text('{}')
    with pytest.raises(ValueError,match='receipt mismatch'): import_publication(root/'latest.json',target,first)
    book['scenarios'][0]['thesis']='updated';publish()
    revised=import_publication(root/'latest.json',target,first+timedelta(hours=2))
    assert revised['plans'][0]['admission_block']=='weekly_source_revision_requires_readmission'
    assert revised['plans'][0]['deployment_id']==a['plans'][0]['deployment_id']


def test_parallel_arms_are_independent_and_keep_same_author_rule(params):
    from bhiksha.integrations.cartographer_weekly import execution_plans
    params['early_admitted_at']=params['admitted_at']
    params['controls']['entry_timing_comparison']='PAIRED'
    baseline,early=execution_plans({'plans':[params]})
    assert baseline['deployment_id']==params['deployment_id']
    assert early['author_trigger']==baseline['trigger']
    assert early['tactical_invalidation']==baseline['tactical_invalidation']
    first=datetime(2026,9,28,13,31,tzinfo=UTC)
    assert observe(frame(minutes=1),early,first)[0]['reason']=='weekly_confirmed'
    assert observe(frame(minutes=1),baseline,first)[0]['reason']=='weekly_waiting_confirmation'
    assert reserve(deployment(early),first,0) is None
    transition(deployment(early),'filled','early-trade')
    later=datetime(2026,9,28,14,9,tzinfo=UTC)
    assert observe(frame(),baseline,later)[0]['reason']=='weekly_confirmed'
    assert reserve(deployment(baseline),later,0) is None
    sibling=deepcopy(early);sibling['deployment_id']='cw-sibling-early1m'
    observe(frame(minutes=1),sibling,first)
    assert reserve(deployment(sibling),first,0)=='weekly_scenario_already_reserved_or_consumed'


def test_pending_capacity_is_per_arm(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    params['controls']['max_open_positions']=1
    observe(frame(),params,now)
    assert reserve(deployment(params),now,0) is None
    other=deepcopy(params)
    other.update(deployment_id='cw-other-early1m',scenario_key='cw-other',entry_arm='early_1m')
    other['controls']['entry_timing_comparison']='PAIRED'
    observe(frame(),other,now)
    assert reserve(deployment(other),now,0) is None


def test_early_arm_switch_revision_and_prospective_boundary(params):
    from bhiksha.integrations.cartographer_weekly import execution_plans
    from bhiksha.strategy.weekly_chart import source_block
    from pathlib import Path
    params['early_admitted_at']='2026-09-28T13:35:30+00:00'
    baseline,early=execution_plans({'plans':[params]})
    now=datetime(2026,9,28,13,36,tzinfo=UTC)
    assert source_block(early,now)=='weekly_early_arm_off'
    assert source_block(baseline,now) is None
    early['controls']['entry_timing_comparison']='PAIRED'
    assert observe(frame(minutes=6),early,now)[0]['reason']=='weekly_waiting_confirmation'
    assert observe(frame(minutes=7),early,now+timedelta(minutes=1))[0]['reason']=='weekly_confirmed'
    atomic_json(Path(params['source_health_path']),{'ok':True,'checked_at':now.isoformat(),'blocked_deployments':[params['deployment_id']]})
    assert source_block(early,now)=='weekly_source_revision_requires_readmission'


def test_status_keeps_untriggered_arm_visible(tmp_path,params):
    from bhiksha.tools.cartographer_weekly import status_rows,HEADERS
    params.update(early_admitted_at=params['admitted_at'],author_profile='TREND_CONTINUATION')
    rows=status_rows({'plans':[params]},DEFAULTS,db_path=str(tmp_path/'absent'))
    assert len(rows)==2 and all(len(r)==len(HEADERS) for r in rows)
    assert [r[17] for r in rows]==['baseline','early_1m']
    assert all(r[12]=='' for r in rows) # no invented trades or P&L


def test_verified_prefix_survives_eviction_and_restart(params):
    now=datetime(2026,9,28,14,9,tzinfo=UTC)
    observe(frame(),params,now)
    _RECOVERED.clear()
    # Earlier verified history has left the rolling buffer.
    later=frame(minutes=78).slice(39)
    data,_=observe(later,params,now+timedelta(minutes=39))
    assert data['missing_history_from'] is None
    assert data['verified_coverage']['invalidation']['through']=='2026-09-28T14:48:00+00:00'


def test_hole_repair_preserves_original_confirmation_time(params):
    now=datetime(2026,9,28,14,48,tzinfo=UTC)
    missing=frame(minutes=78).slice(1)
    data,_=observe(missing,params,now)
    assert data['reason']=='weekly_confirmation_data_gap'
    assert data['missing_history_from']=='2026-09-28T13:30:00+00:00'
    data,_=observe(frame(minutes=78),params,now)
    assert data['missing_history_from'] is None
    # Repair is not a new opportunity with a fresh retry window.
    assert data['confirmation_at']=='2026-09-28T14:09:00+00:00'
    assert data['reason']=='weekly_retry_window_expired'


def test_missing_entire_session_is_not_silently_valid(params):
    now=datetime(2026,9,29,14,9,tzinfo=UTC)
    data,_=observe(frame('2026-09-29'),params,now)
    assert data['missing_history_from']=='2026-09-28T13:30:00+00:00'
    assert 'confirmation_at' not in data


def test_repair_reveals_invalidation_before_later_trigger(params):
    now=datetime(2026,9,28,14,48,tzinfo=UTC)
    observe(frame(minutes=78).slice(39),params,now)
    repaired=pl.concat([frame(price=580.0),frame(minutes=78).slice(39)])
    data,status=observe(repaired,params,now)
    assert status=='invalidated' and data['reason']=='weekly_invalidated'


@pytest.mark.asyncio
async def test_runtime_repairs_same_source_and_throttles_unresolved_hole(params,monkeypatch):
    from bhiksha.app import runtime as module
    from unittest.mock import AsyncMock
    from bhiksha.domain.models import Bar
    fixed=datetime(2026,9,28,14,9,1,tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return fixed
    monkeypatch.setattr(module,'datetime',Clock)
    original=frame()
    bars=[Bar(symbol='SPY',timestamp=r['timestamp'],open=r['close'],high=r['close'],
              low=r['close'],close=r['close'],volume=100) for r in original.iter_rows(named=True)]
    source=SimpleNamespace(warm_start=AsyncMock(return_value=bars),close=AsyncMock())
    runtime=SimpleNamespace(_weekly_repair_at={},_live_bar_source=lambda:source,
        provider_config=SimpleNamespace(underlying_live_primary='schwab'))
    events=SimpleNamespace(append=AsyncMock())
    repaired=await module.BhikshaRuntime._repair_weekly_frame(runtime,'SPY',original.slice(1),[deployment(params)],events)
    data,_=observe(repaired,params,fixed)
    assert data['reason']=='weekly_confirmed'
    assert source.warm_start.await_args.args[1]==datetime(2026,9,28,13,30,tzinfo=UTC)
    assert source.close.await_count==1
    assert events.append.await_args.args[1]['provider']=='schwab'
    # A later hole remains explicitly blocked; do not hammer the provider every bar.
    params['deployment_id']='new-lane'
    await module.BhikshaRuntime._repair_weekly_frame(runtime,'SPY',original.slice(1),[deployment(params)],events)
    assert source.warm_start.await_count==1
    assert observe(original.slice(1),params,fixed)[0]['reason']=='weekly_confirmation_data_gap'
