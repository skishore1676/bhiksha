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
    p={**params,'author_profile':'TREND_CONTINUATION','setup_type':'upside_breakout'}
    atomic_json(root/'admissions.json',{'plans':[p]})
    control={**DEFAULTS,'compare_exits':'trend_continuation_balanced'}
    rows=build_rows({'cartographer_weekly':control},root,str(tmp_path/'db'))
    name='trend_continuation_balanced'
    catalog={name:ExitProfileConfig(exit_profile_id=name,trade_archetype='TREND_CONTINUATION',exit_family='staged_r_ladder',target_1_r=1,target_2_r=2,target_1_quantity=.6,initial_stop_pct=.3,disaster_stop_pct=.35,no_progress_seconds=2700,giveback_policy='OFF',breakeven_after_t1=True,eod_flat=True,hard_flat_time_et='15:55')}
    d=compile_active_plan_from_rows(rows=rows,strategy_catalog_path=tmp_path/'catalog',exit_profiles_catalog=catalog).plan.deployments[0]
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
    (run/'weekly-book.json').write_text('{}')
    with pytest.raises(ValueError,match='receipt mismatch'): import_publication(root/'latest.json',target,first)
    book['scenarios'][0]['thesis']='updated';publish()
    revised=import_publication(root/'latest.json',target,first+timedelta(hours=2))
    assert revised['plans'][0]['admission_block']=='weekly_source_revision_requires_readmission'
    assert revised['plans'][0]['deployment_id']==a['plans'][0]['deployment_id']
