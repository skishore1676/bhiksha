"""Source-only regressions for quote provenance, retry timing and report attribution."""
import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.execution.pricing import select_entry_limit
from bhiksha.execution.quote_lineage import extract_public_quote_timestamp, quote_timestamp_evidence
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.ops.daily_report import build_daily_report, render_daily_report_ryg_markdown
from bhiksha.ops.trade_observation import reporting_exclusion, summarize_evaluation_coverage
from bhiksha.ops.weekly_scorecard import build_weekly_scorecard, render_weekly_scorecard_telegram_summary
from bhiksha.ops.weekly_trading_decisions import build_trading_decision_export, _event_only_observations
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteTradeStateRepository
from bhiksha.state.position_tracker import PositionTracker
from bhiksha.strategy.weekly_chart import observe, reserve, state, _RECOVERED
from test_cartographer_weekly import params, frame, deployment
from test_execution_planner import StubChainService, StubOrderManager, StubRiskManager, RecordingAllowedCashGuard, _enabled_deployment
from test_entry_exit_refactor import deployment as paper_deployment

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


class Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def freeze(monkeypatch):
    Clock.current = NOW
    monkeypatch.setattr('bhiksha.execution.planner.datetime', Clock)
    monkeypatch.setattr('bhiksha.execution.pricing.datetime', Clock)


def quote(at=NOW, **kwargs):
    return PublicQuote('QQQ260330P00558000', bid=2.70, ask=2.90, open_interest=550,
                       quote_timestamp=at.isoformat(), quote_timestamp_field='quoteTimestamp', **kwargs)


@pytest.mark.parametrize('payload,status', [
    ({'quoteTimestamp': NOW.timestamp()}, 'current'),
    ({'quoteTimestamp': NOW.timestamp() * 1000}, 'current'),
    ({'quoteTimestamp': NOW.isoformat()}, 'current'),
    ({'quoteTimestamp': (NOW - timedelta(seconds=5)).isoformat()}, 'current'),
    ({'quoteTimestamp': (NOW - timedelta(seconds=5, microseconds=1)).isoformat()}, 'current'),
    ({'quoteTimestamp': (NOW - timedelta(seconds=8)).isoformat()}, 'current'),
    ({'quoteTimestamp': (NOW - timedelta(seconds=8, microseconds=1)).isoformat()}, 'stale'),
    ({'quoteTimestamp': (NOW - timedelta(seconds=10)).isoformat()}, 'stale'),
    ({'quoteTimestamp': (NOW + timedelta(microseconds=1)).isoformat()}, 'unproven'),
    ({'bidTimestamp': NOW.isoformat()}, 'missing'),
    ({'quoteTimestamp': NOW.isoformat(), 'bidTimestamp': 'bad'}, 'unproven'),
    ({'quoteTimestamp': NOW.isoformat(), 'askTimestamp': (NOW + timedelta(seconds=1)).isoformat()}, 'unproven'),
    ({'quoteTimestamp': NOW.isoformat(), 'bidTimestamp': (NOW - timedelta(seconds=6)).isoformat()}, 'current'),
    ({'quoteTimestamp': NOW.isoformat(), 'bidTimestamp': (NOW - timedelta(seconds=8, microseconds=1)).isoformat()}, 'stale'),
    ({'bidTimestamp': NOW.isoformat(), 'askTimestamp': (NOW + timedelta(seconds=1)).isoformat()}, 'unproven'),
    ({'timestamp': NOW.isoformat()}, 'missing'),
])
def test_quote_gate_proves_provider_timestamp_and_sides(payload, status):
    ts, field, bid_at, ask_at = extract_public_quote_timestamp(payload)
    q = PublicQuote('OPT', bid=2, ask=2.2, open_interest=100,
                    quote_timestamp=ts, quote_timestamp_field=field, bid_timestamp=bid_at, ask_timestamp=ask_at)
    result = select_entry_limit(q, {'entry_pricing_mode': 'price_seeking'}, observed_at=NOW)
    assert result.approved == (status == 'current')
    evidence = result.evidence()
    assert evidence['quote_timestamp_status'] == status
    assert evidence['quote_observed_at'] == NOW.isoformat()
    assert evidence['quote_max_age_seconds'] == 8


def test_inconsistent_effective_side_timestamp_cannot_pass_gate():
    q = quote()
    q.quote_timestamp_field = 'bidTimestamp+askTimestamp'
    q.bid_timestamp = (NOW - timedelta(seconds=1)).isoformat()
    q.ask_timestamp = NOW.isoformat()
    assert quote_timestamp_evidence(q, NOW)['quote_timestamp_status'] == 'unproven'


def test_planner_accepts_seven_second_quote_without_refresh(monkeypatch):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = 'price_seeking'
    manager = StubOrderManager()
    manager.get_option_quote = AsyncMock(return_value=quote(NOW - timedelta(seconds=7)))
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager,
                               position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', NOW, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=True, simulate_only=True))
    assert plan.risk_reasons == ['approved']
    assert manager.get_option_quote.await_count == 1
    assert plan.risk_details['entry_pricing']['quote_max_age_seconds'] == 8


@pytest.mark.parametrize('age,filled', [(6, True), (8, True), (8.000001, False), (10, False)])
def test_paper_fill_uses_eight_second_quote_boundary(age, filled):
    async def run():
        dep = paper_deployment()
        decision = SignalDecision(dep.deployment_id, dep.symbol, NOW, True, SignalDirection.SHORT, [], {})
        plan = TradePlan('paper-age', dep.deployment_id, dep.symbol, SignalDirection.SHORT,
                         'QQQ260330P00558000', 1, 2.90, ['approved'], entry_timestamp=NOW)
        q = quote(NOW + timedelta(seconds=1))
        manager = SimpleNamespace(get_option_quote=AsyncMock(return_value=q), close=AsyncMock())
        planner = SimpleNamespace(position_tracker=PositionTracker(), order_manager=manager, close=AsyncMock())
        supervisor = ExecutionSupervisor(planner=planner, exit_edge_recorder=MagicMock())
        supervisor._paper_entries[plan.trade_id] = (dep, decision, plan, NOW, NOW + timedelta(seconds=30))
        supervisor.lifecycle_store.begin_entry(dep.symbol, dep.deployment_id, order_id='PAPER_PENDING')
        await supervisor.poll_paper_entries(now=NOW + timedelta(seconds=1 + age))
        assert planner.position_tracker.total_open_positions == int(filled)
        assert (plan.trade_id not in supervisor._paper_entries) == filled
        assert plan.risk_details['paper_quote_timing']['quote_max_age_seconds'] == 8
    asyncio.run(run())


@pytest.mark.parametrize('case,reads,reason', [
    ('recover', 2, 'approved'), ('nonadvancing', 2, 'public_quote_stale_or_unproven'),
    ('missing', 2, 'approved'), ('crossed', 1, 'public_quote_crossed_bid_ask'),
    ('oi', 1, 'public_open_interest_missing'), ('nonfinite', 1, 'public_quote_nonfinite'),
    ('cancelled', 2, 'entry_retry_cancelled'), ('window', 2, 'execution_window_blocked'),
    ('budget', 2, 'insufficient_budget_for_single_contract'),
])
def test_planner_refresh_is_bounded_and_preserves_safety(monkeypatch, case, reads, reason):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = 'price_seeking'
    dep.execution.entry_window_start_et = '09:35'
    dep.execution.entry_window_end_et = '15:45'
    dep.risk.max_trade_premium_usd = 300
    first = quote(NOW - timedelta(seconds=9))
    if case == 'missing': first.quote_timestamp = None
    if case == 'crossed': first.bid, first.ask = 3, 2.9
    if case == 'oi': first.open_interest = 0
    if case == 'nonfinite': first.bid = float('nan')
    fresh = quote()
    if case == 'budget': fresh.bid, fresh.ask = 8.9, 9.1
    cancelled = False
    manager = StubOrderManager()
    calls = 0
    async def get_quote(symbol):
        nonlocal calls, cancelled
        calls += 1
        if calls == 1: return first
        if case == 'cancelled': cancelled = True
        if case == 'window': Clock.current = NOW.replace(hour=20)
        return first if case == 'nonadvancing' else fresh
    manager.get_option_quote = get_quote
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager, position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', NOW, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=True, simulate_only=True,
                                         entry_guard=lambda: 'entry_retry_cancelled' if cancelled else None))
    assert calls == reads
    assert reason in plan.risk_reasons
    assert manager.place_entry_order_calls == 0
    if case in {'recover', 'missing', 'nonadvancing'}:
        pricing = plan.risk_details['entry_pricing']
        assert len(pricing['quote_attempts']) == 2
        assert pricing['quote_refresh_status'] == ('unavailable' if case == 'nonadvancing' else 'recovered')
        assert pricing['quote_attempts'][0]['quote_received_at'] == NOW.isoformat()
        assert pricing['quote_attempts'][0]['fetch_to_pricing_seconds'] == 0


def test_refresh_then_reservation_window_expiry_releases_cash_and_risk(monkeypatch):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = 'price_seeking'
    dep.execution.entry_window_end_et = '15:45'
    manager = StubOrderManager()
    manager.get_option_quote = AsyncMock(side_effect=[quote(NOW - timedelta(seconds=9)), quote(), quote(NOW - timedelta(seconds=9)), quote(NOW - timedelta(seconds=9))])
    original_preflight = manager.preflight_entry
    async def aligned_preflight(symbol, limit, quantity):
        result = await original_preflight(symbol, limit, quantity)
        result.payload["limitPrice"] = str(limit)
        return result
    manager.preflight_entry = aligned_preflight
    risk = StubRiskManager()
    original_reserve = risk.reserve_sized_entry
    async def reserve_risk(**kwargs):
        result = await original_reserve(**kwargs)
        Clock.current = NOW.replace(hour=20)
        return result
    risk.reserve_sized_entry = reserve_risk
    cash = RecordingAllowedCashGuard()
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager, risk_manager=risk,
                               cash_guard=cash, position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', NOW, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=False))
    assert plan.risk_reasons == ['execution_window_blocked']
    assert manager.place_entry_order_calls == 0
    assert risk.release_calls == [plan.trade_id]
    assert ('release', plan.trade_id) in cash.calls


def test_early_retry_anchor_preserves_confirmation_across_gap_restart_and_next_day(params):
    params['trigger']['timeframe'] = '1m'
    params['entry_window_start_et'] = '09:35'
    before = datetime(2026, 9, 28, 13, 31, tzinfo=UTC)
    data, _ = observe(frame(minutes=1), params, before)
    assert data['reason'] == 'weekly_waiting_entry_window'
    assert data['confirmation_at'] == before.isoformat()
    anchor = datetime(2026, 9, 28, 13, 35, tzinfo=UTC)
    assert data['retry_started_at'] == anchor.isoformat()
    _RECOVERED.clear()
    gapped = frame(minutes=5).slice(1)
    # Existing proved prefix survives; a new unresolved gap must not reset TTL.
    data, _ = observe(gapped, params, anchor)
    assert data['confirmation_at'] == before.isoformat()
    assert data['retry_started_at'] == anchor.isoformat()
    later = anchor + timedelta(seconds=params['controls']['retry_seconds'] + 1)
    data, _ = observe(frame(minutes=15), params, later)
    assert data['reason'] == 'weekly_retry_window_expired'
    assert data['retry_started_at'] == anchor.isoformat()


def test_daily_retry_anchor_uses_next_session_start_without_late_extension(params):
    params['trigger']['timeframe'] = params['tactical_invalidation']['timeframe'] = 'daily'
    params['entry_window_start_et'] = '10:00'
    close = datetime(2026, 9, 28, 20, tzinfo=UTC)
    observe(frame(minutes=390), params, close)
    from bhiksha.integrations.cartographer_weekly import atomic_json
    from pathlib import Path
    atomic_json(Path(params['source_health_path']), {'ok': True, 'checked_at': '2026-09-29T12:30:00+00:00'})
    import polars as pl
    later = datetime(2026, 9, 29, 14, 11, tzinfo=UTC)
    bars = pl.concat([frame(minutes=390), frame('2026-09-29', minutes=41)])
    data, _ = observe(bars, params, later)
    assert data['retry_started_at'] == '2026-09-29T14:00:00+00:00'
    assert data['reason'] == 'weekly_retry_window_expired'


def test_weekly_frozen_legacy_execution_window_drives_anchor_without_payload_mutation(params):
    params['trigger']['timeframe'] = '1m'
    data, _ = observe(frame(minutes=5), params, datetime(2026, 9, 28, 13, 35, tzinfo=UTC))
    assert reserve(deployment(params), datetime(2026, 9, 28, 13, 35, tzinfo=UTC), 0) is None
    frozen = {'strategy': {'params': deepcopy(params)}, 'execution': {'entry_window_start_et': '10:00'},
              'exit': {'exit_policy_sha256': 'keep'}, 'source': {'metadata': {'cohort_contract_sha256': 'keep'}}}
    with state(params) as db:
        db.execute('UPDATE weekly_chart_state SET frozen_deployment=? WHERE deployment_id=?', (json.dumps(frozen), params['deployment_id']))
    data, _ = observe(frame(minutes=31), params, datetime(2026, 9, 28, 14, 1, tzinfo=UTC))
    assert data['retry_started_at'] == '2026-09-28T14:00:00+00:00'
    with state(params) as db:
        saved = json.loads(db.execute('SELECT frozen_deployment FROM weekly_chart_state WHERE deployment_id=?', (params['deployment_id'],)).fetchone()[0])
    assert saved == frozen


@pytest.mark.parametrize('reason,outcome', [('weekly_confirmation_data_gap', 'MISSING'), ('weekly_underlying_stale', 'MISSING'),
                                         ('weekly_waiting_confirmation', 'NO_SIGNAL'), ('weekly_waiting_entry_window', 'BLOCKED')])
def test_report_outcome_separates_missing_coverage_from_no_trigger_and_gated_trigger(tmp_path, reason, outcome):
    features = {'confirmation_at': NOW.isoformat()} if outcome == 'BLOCKED' else {}
    event = {'event_type': 'signal_evaluation', 'created_at': NOW.isoformat(), 'event_id': 1,
             'payload': {'deployment_id': 'weekly', 'signal': False, 'reason': [reason], 'features': features}}
    result = _event_only_observations([event], facts=[], existing=[], db_path=tmp_path/'db', exported_at=NOW.isoformat())
    assert result[0]['observation_outcome'] == outcome
    coverage = summarize_evaluation_coverage([event], [SimpleNamespace(deployment_id='weekly', enabled=True),
                                                     SimpleNamespace(deployment_id='missing-lane', enabled=True)])
    assert {r['deployment_id']: r['status'] for r in coverage['lanes']}['missing-lane'] == 'unknown'
    assert coverage['expected_cadence_proved'] is False
    assert coverage['status'] != 'complete'


HISTORICAL = dict(trade_id='edb45772-3304-4623-ad91-b6ed1c8f4a59',
                  deployment_id='strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3',
                  symbol='IWM', option_symbol='IWM260929C00285000', quantity=100, entry_price=.52,
                  entry_timestamp=datetime.fromisoformat('2026-09-25T13:54:52.525000+00:00'),
                  entry_order_id='98f8868b-438f-4c46-a509-75605218b52c', status='closed')


@pytest.mark.parametrize('mixed', [False, True])
def test_historical_operator_case_is_excluded_in_all_report_paths_and_raw_truth_preserved(tmp_path, mixed):
    db_path = tmp_path/'db'
    repo = SQLiteTradeStateRepository(str(db_path), backend=SQLiteBackend(str(db_path)))
    async def seed():
        await repo.upsert_trade(TradeRecord(**HISTORICAL))
        await repo.mark_closed(HISTORICAL['trade_id'], exit_price=.52, exit_filled_quantity=100,
                               exit_filled_at=datetime(2026, 9, 25, 15, tzinfo=UTC), exit_order_status='FILLED')
        if mixed:
            await repo.upsert_trade(TradeRecord(**{**HISTORICAL, 'trade_id': 'different-valid-trade', 'quantity': 1}))
            await repo.mark_closed('different-valid-trade', exit_price=.72, exit_filled_quantity=1,
                                   exit_filled_at=datetime(2026, 9, 25, 15, tzinfo=UTC), exit_order_status='FILLED')
    asyncio.run(seed())
    daily = build_daily_report(db_path, trading_date='2026-09-25')
    row = next(r for r in daily['trades'] if r['trade_id'] == HISTORICAL['trade_id'])
    assert (row['quantity'], row['entry_price'], row['exit_price']) == (100, .52, .52)
    assert row['realized_pnl_usd'] is None and row['pnl_eligible'] is False
    assert row['operator_case_status'] == 'closed'
    summary = daily['trade_summary']
    assert summary['live_excluded_count'] == 1
    assert summary['live_missing_exit_truth_count'] == 0
    assert summary['live_realized_pnl_usd'] is None
    assert summary['live_known_subtotal_pnl_usd'] == (20 if mixed else None)
    assert 'unknown (1 excluded' in render_daily_report_ryg_markdown(daily)
    weekly = build_weekly_scorecard(db_path, week_start='2026-09-21', week_end='2026-09-25')
    for bucket in (weekly['headline']['live'], weekly['live_cumulative']):
        assert bucket['excluded_count'] == 1
        assert bucket['total_pnl_usd'] is None
        assert bucket['known_subtotal_pnl_usd'] == (20 if mixed else None)
    assert weekly['headline']['live']['closed'] == int(mixed)
    assert weekly['profile_vs_legacy']['live']['legacy']['n'] == int(mixed)
    render_weekly_scorecard_telegram_summary(weekly)  # None total must remain renderable.
    export = build_trading_decision_export(db_path, through=date(2026, 9, 25), deployments=None, report_dir=tmp_path/'reports')
    assert all(f['trade_id'] != HISTORICAL['trade_id'] for f in export['facts'])
    observation = next(o for o in export['observations'] if o['trade_id'] == HISTORICAL['trade_id'])
    assert observation['observation_outcome'] == 'EXCLUDED'
    assert observation['realized_pnl_usd'] is None and observation['operator_case_status'] == 'closed'
    import sqlite3
    with sqlite3.connect(db_path) as db:
        raw = db.execute('SELECT quantity, entry_price, exit_price FROM trade_sessions WHERE trade_id=?', (HISTORICAL['trade_id'],)).fetchone()
    assert raw == (100, .52, .52)


@pytest.mark.parametrize('key', ['trade_id', 'deployment_id', 'symbol', 'option_symbol', 'entry_order_id', 'entry_timestamp'])
def test_exclusion_does_not_match_identity_collision(key):
    raw = {**HISTORICAL, 'entry_timestamp': HISTORICAL['entry_timestamp'].isoformat()}
    assert reporting_exclusion(raw)
    raw[key] = 'other'
    assert reporting_exclusion(raw) is None


def test_refresh_exception_is_terminal_and_cannot_submit(monkeypatch):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = 'price_seeking'
    manager = StubOrderManager()
    manager.get_option_quote = AsyncMock(side_effect=[quote(NOW - timedelta(seconds=9)), RuntimeError('provider unavailable')])
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager, position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', NOW, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=False))
    assert manager.get_option_quote.await_count == 2
    assert manager.place_entry_order_calls == 0
    timing = plan.risk_details['entry_pricing']
    assert timing['quote_refresh_status'] == 'unavailable'
    assert timing['provider_timestamp_advanced'] is None
    assert timing['quote_attempts'][-1] == {'quote_timestamp_status': 'unavailable', 'error': 'RuntimeError'}


@pytest.mark.parametrize('stage', ['cash_reject', 'risk_reject', 'risk_delay'])
def test_refresh_preserves_downstream_caps_and_delayed_final_quote_gate(monkeypatch, stage):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = 'price_seeking'
    manager = StubOrderManager()
    manager.get_option_quote = AsyncMock(side_effect=[quote(NOW - timedelta(seconds=9)), quote(), quote(NOW - timedelta(seconds=9)), quote(NOW - timedelta(seconds=9))])
    original_preflight = manager.preflight_entry
    async def preflight(symbol, limit, quantity):
        result = await original_preflight(symbol, limit, quantity)
        result.payload['limitPrice'] = str(limit)
        return result
    manager.preflight_entry = preflight
    risk = StubRiskManager(allowed=stage != 'risk_reject', reason='risk_cap_blocked')
    reserve_risk = risk.reserve_sized_entry
    async def delayed_reserve(**kwargs):
        result = await reserve_risk(**kwargs)
        if stage == 'risk_delay': Clock.current += timedelta(seconds=9)
        return result
    risk.reserve_sized_entry = delayed_reserve
    cash = RecordingAllowedCashGuard()
    if stage == 'cash_reject':
        from bhiksha.risk.cash_guard import CashGuardResult
        cash.reserve_entry = AsyncMock(return_value=CashGuardResult(enforced=True, blocked=True, reason='cash_guard_blocked'))
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager, risk_manager=risk,
                               cash_guard=cash, position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', NOW, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=False))
    assert plan.risk_reasons == [{'cash_reject': 'cash_guard_blocked', 'risk_reject': 'risk_cap_blocked',
                                 'risk_delay': 'public_quote_stale_or_unproven'}[stage]]
    assert manager.get_option_quote.await_count == (4 if stage == 'risk_delay' else 2)
    assert manager.place_entry_order_calls == 0
    if stage == 'risk_delay':
        assert risk.release_calls
        assert all(('release', trade_id) in cash.calls for trade_id in risk.release_calls)


def test_historical_startup_inventory_wins_over_current_manifest():
    events = [{'event_type': 'startup_config', 'payload': {'deployments': [{'deployment_id': 'historical', 'enabled': True}]}},
              {'event_type': 'signal_evaluation', 'payload': {'deployment_id': 'historical', 'signal': False, 'reason': ['quiet']}}]
    report = summarize_evaluation_coverage(events, [SimpleNamespace(deployment_id='new-lane', enabled=True)])
    assert [r['deployment_id'] for r in report['lanes']] == ['historical']
    assert report['inventory_source'] == 'startup_config'
    assert report['status'] == 'partial'


def test_paper_poll_rechecks_window_after_quote_io_and_rejects_bad_side_provenance(monkeypatch):
    freeze(monkeypatch)
    monkeypatch.setattr('bhiksha.execution.supervisor.datetime', Clock)
    async def run():
        dep = paper_deployment()
        dep.execution.entry_window_end_et = '15:45'
        decision = SignalDecision(dep.deployment_id, dep.symbol, NOW, True, SignalDirection.SHORT, [], {})
        plan = TradePlan('paper-case', dep.deployment_id, dep.symbol, SignalDirection.SHORT,
                         'QQQ260330P00558000', 1, 2.90, [], entry_timestamp=NOW)
        q = quote(NOW + timedelta(seconds=1))
        q.bid_timestamp = 'invalid'
        manager = SimpleNamespace(get_option_quote=AsyncMock(return_value=q), close=AsyncMock())
        planner = SimpleNamespace(position_tracker=PositionTracker(), order_manager=manager, close=AsyncMock())
        recorder = MagicMock()
        supervisor = ExecutionSupervisor(planner=planner, exit_edge_recorder=recorder,
                                         event_repository=SimpleNamespace(append=AsyncMock()))
        supervisor._paper_entries[plan.trade_id] = (dep, decision, plan, NOW, NOW + timedelta(hours=8))
        supervisor.lifecycle_store.begin_entry(dep.symbol, dep.deployment_id, order_id='PAPER_PENDING')
        Clock.current = NOW + timedelta(seconds=2)
        await supervisor.poll_paper_entries()
        assert plan.trade_id in supervisor._paper_entries
        assert plan.risk_details['paper_quote_timing']['quote_timestamp_status'] == 'unproven'
        assert not recorder.prepare_registration.called
        async def await_quote(symbol):
            Clock.current = NOW.replace(hour=20)
            q.bid_timestamp = None
            q.quote_timestamp = Clock.current.isoformat()
            return q
        manager.get_option_quote = await_quote
        await supervisor.poll_paper_entries()
        assert not supervisor._paper_entries
        assert not recorder.prepare_registration.called
    asyncio.run(run())


def test_compiler_uses_final_custom_window_and_preserves_pending_frozen_policy(tmp_path, params):
    from bhiksha.integrations.cartographer_weekly import build_rows, atomic_json, DEFAULTS
    from bhiksha.active_plan.compiler import compile_active_plan_from_rows
    from test_entry_exit_refactor import profile
    root = tmp_path/'admissions'; root.mkdir()
    atomic_json(root/'source_health.json', {'ok': True, 'checked_at': '2026-09-28T12:30:00+00:00'})
    atomic_json(root/'admissions.json', {'plans': [{**params, 'author_profile': 'TREND_CONTINUATION', 'setup_type': 'upside_breakout'}]})
    policy = profile('trend_continuation_balanced')
    defaults = {'cartographer_weekly': {**DEFAULTS, 'compare_exits': 'trend_continuation_balanced'}}
    rows = build_rows(defaults, root, params['state_db'])
    rows[0].execution_overrides['entry_window_start_et'] = '10:00'
    def compile_rows():
        return compile_active_plan_from_rows(rows=rows, strategy_catalog_path=tmp_path/'catalog',
                                            exit_profiles_catalog={'trend_continuation_balanced': policy}).plan.deployments[0]
    dep = compile_rows()
    assert dep.strategy.params['entry_window_start_et'] == '10:00'
    dep.source.metadata['cohort_contract_sha256'] = 'frozen-cohort'
    dep.source.metadata['authorization_sha256'] = 'frozen-authorization'
    at = datetime(2026, 9, 28, 14, 9, tzinfo=UTC)
    observe(frame(), dep.strategy.params, at)
    assert reserve(dep, at, 0) is None
    old = dep.model_dump(mode='json')
    old["strategy"]["params"]["admission_block"] = None
    rows[0].execution_overrides['entry_window_start_et'] = '11:00'
    frozen = compile_rows()
    assert frozen.model_dump(mode='json') == old
    assert frozen.execution.entry_window_start_et == frozen.strategy.params['entry_window_start_et'] == '10:00'


@pytest.mark.parametrize('safety_red', [False, True])
def test_daily_data_gap_status_is_qualified_and_preserves_safety_red(tmp_path, safety_red):
    from bhiksha.persistence.sqlite import SQLiteEventRepository
    db_path = tmp_path/'db'
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)
    async def seed():
        await events.append('startup_config', {'deployments': [{'deployment_id': 'weekly', 'symbol': 'SPY', 'enabled': True}]})
        await events.append('signal_evaluation', {'deployment_id': 'weekly', 'symbol': 'SPY', 'signal': False,
                                                 'reason': ['weekly_confirmation_data_gap']})
        if safety_red:
            await events.append('runtime_issue', {'category': 'dead_lane'})
    asyncio.run(seed())
    report = build_daily_report(db_path, trading_date=datetime.now(UTC).date())
    assert report['evaluation_coverage']['status'] == 'incomplete'
    assert report['coverage_status']['level'] == 'YELLOW'
    assert report['status']['level'] == ('RED' if safety_red else 'YELLOW')


def test_feed_dns_receipt_remains_visible_as_coverage_issue_without_order_escalation():
    issue = {'event_id': 5, 'event_type': 'runtime_issue', 'created_at': NOW.isoformat(),
             'payload': {'category': 'data', 'stage': 'market_data_provider', 'symbol': 'SPY', 'error': 'DNS lookup failed'}}
    ordinary_order = {'event_id': 6, 'event_type': 'runtime_issue', 'payload': {'category': 'order', 'stage': 'entry', 'error': 'order failed'}}
    report = summarize_evaluation_coverage([issue, ordinary_order], [SimpleNamespace(deployment_id='weekly', symbol='SPY', enabled=True)])
    assert report['status'] == 'incomplete'
    assert report['source_issues'][0]['error'] == 'DNS lookup failed'
    assert len(report['source_issues']) == 1


def test_weekly_invalidation_does_not_hide_unresolved_coverage_gap(params):
    params['tactical_invalidation']['timeframe'] = '1m'
    params['tactical_invalidation']['price'] = 601
    now = datetime(2026, 9, 28, 14, 9, tzinfo=UTC)
    bars = frame().filter(__import__('polars').col('timestamp') != datetime(2026, 9, 28, 13, 35, tzinfo=UTC))
    data, status = observe(bars, params, now)
    assert status == 'invalidated' and data['reason'] == 'weekly_invalidated'
    assert data['missing_history_from'] and data['evaluation_coverage'] == 'incomplete'


def test_weekly_pending_ttl_crossing_during_refresh_refuses_submission(monkeypatch, params):
    freeze(monkeypatch)
    from bhiksha.config.models import StrategySpec
    from bhiksha.strategy.weekly_chart import pending_block
    # Confirmation 09:39 with a ten-minute retry ends 09:49, before refreshed read returns.
    Clock.current = datetime(2026, 9, 28, 14, 18, 59, tzinfo=UTC)
    observe(frame(minutes=48), params, Clock.current)
    d = deployment(params)
    assert reserve(d, Clock.current, 0) is None
    assert pending_block(d, Clock.current) is None
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    params['deployment_id'] = dep.deployment_id
    # Create the planner deployment's own pending admission from the same confirmation.
    observe(frame(minutes=48), params, Clock.current)
    assert reserve(deployment(params), Clock.current, 0) == 'weekly_scenario_already_reserved_or_consumed'
    params['scenario_key'] = 'another'
    with state(params) as db:
        db.execute('UPDATE weekly_chart_state SET scenario_key=? WHERE deployment_id=?', ('another', params['deployment_id']))
    assert reserve(deployment(params), Clock.current, 0) is None
    dep.strategy = StrategySpec(key='weekly_chart', params=params)
    dep.execution.entry_pricing_mode = 'price_seeking'
    dep.execution.entry_window_start_et = '09:35'
    dep.execution.entry_window_end_et = '15:45'
    manager = StubOrderManager()
    reads = 0
    async def fetch(symbol):
        nonlocal reads
        reads += 1
        if reads == 1: return quote(Clock.current - timedelta(seconds=9))
        Clock.current += timedelta(seconds=2)
        return quote(Clock.current)
    manager.get_option_quote = fetch
    planner = ExecutionPlanner(chain_service=StubChainService(), order_manager=manager, position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id, 'QQQ', Clock.current, True, SignalDirection.SHORT, [], {})
    plan = asyncio.run(planner.plan_entry(dep, decision, dry_run=True, simulate_only=True))
    assert plan.risk_reasons == ['weekly_retry_window_expired']
    assert reads == 2 and manager.place_entry_order_calls == 0

@pytest.mark.parametrize('price,headroom,expected', [(0.85,165.98,5),(0.89,165.98,5),(0.77,165.98,6),(0.82,165.98,5),(0.85,29.75,1),(0.85,29.74,0),(0.85,59.50,2)])
def test_adaptive_sizing_uses_real_headroom_and_whole_quantity_rounding(tmp_path, price, headroom, expected):
    from test_risk_manager import _manager, _settings, _put_budget, NOW as RISK_NOW
    manager, _ = _manager(tmp_path, now=RISK_NOW, settings=_settings(max_daily_drawdown_pct=7.5,
                                flatten_daily_drawdown_pct=10, max_open_positions_per_cluster=0))
    _put_budget(manager, headroom / 0.075)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.exit.stop_loss_pct = 0.35
    planner = ExecutionPlanner(risk_manager=manager, order_manager=StubOrderManager())
    async def run():
        qty, reason, _ = await planner._fit_live_quantity(trade_id='fit', deployment=dep,
            timestamp=RISK_NOW, price=price, upper_quantity=20, premium_cap=4000)
        assert qty == expected
        assert reason == (None if expected else 'risk_prospective_loss_headroom_exceeded')
        assert await manager.trade_state_repository.get_active_entry_risk_reservations() == []
    asyncio.run(run())


def test_smh_settled_cash_fit_and_advisory_does_not_reserve(tmp_path, monkeypatch):
    from test_execution_planner import _cash_guard
    monkeypatch.setenv('BHIKSHA_CASH_GUARD_MODE','on')
    monkeypatch.setenv('BHIKSHA_CASH_GUARD_BUFFER_PCT','0')
    manager=StubOrderManager()
    manager.get_portfolio=AsyncMock(return_value={'buyingPower':{'cashOnlyBuyingPower':'693.03'}})
    cash=_cash_guard(manager,tmp_path)
    planner=ExecutionPlanner(cash_guard=cash,order_manager=manager)
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    async def run():
        qty, reason, _=await planner._fit_live_quantity(trade_id='fit',deployment=dep,timestamp=NOW,
            price=2.55,upper_quantity=5,premium_cap=1300)
        assert (qty,reason)==(2,None)
        assert await cash.repository.get_reservation('fit') is None
        await cash.reserve_entry(trade_id='other',required_cash=500,timestamp=NOW)
        qty, reason, _=await planner._fit_live_quantity(trade_id='fit',deployment=dep,timestamp=NOW,
            price=2.55,upper_quantity=5,premium_cap=1300)
        assert qty==0 and reason=='insufficient_internal_settled_cash_budget'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['fees','price','stale'])
def test_live_sizing_repreflights_actual_quantity_and_final_quote(monkeypatch, tmp_path, change):
    from test_execution_planner import _cash_guard
    from bhiksha.execution.order_manager import PreflightCheck
    freeze(monkeypatch)
    monkeypatch.setenv('BHIKSHA_CASH_GUARD_MODE','on')
    monkeypatch.setenv('BHIKSHA_CASH_GUARD_BUFFER_PCT','0')
    dep=_enabled_deployment('market_impulse_qqq_short_v1')
    if change == 'stale':
        dep.execution.entry_pricing_mode = 'price_seeking'
        dep.execution.entry_pricing_spread_fraction = 0.75
    dep.risk.max_trade_premium_usd=2000
    dep.risk.max_contracts=10
    manager=StubOrderManager()
    manager.get_option_quote=AsyncMock(return_value=quote())
    manager.get_portfolio=AsyncMock(return_value={'buyingPower':{'cashOnlyBuyingPower':'570'}})
    calls=[]
    async def preflight(symbol,price,quantity):
        calls.append(quantity)
        if change=='stale': Clock.current=NOW+timedelta(seconds=9)
        normalized=price if change=='stale' else 3.0 if change=='price' else 2.85
        return PreflightCheck(payload={'limitPrice':str(normalized)},current_increment=.01,
                             buying_power_requirement=normalized*quantity*100+0.10,estimated_cost=None)
    manager.preflight_entry=preflight
    cash=_cash_guard(manager,tmp_path)
    risk=StubRiskManager()
    planner=ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,
        cash_guard=cash,risk_manager=risk,position_tracker=PositionTracker())
    decision=SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{})
    plan=asyncio.run(planner.plan_entry(dep,decision,dry_run=False))
    assert calls==([2] if change=='stale' else [2,1])
    if change=='stale':
        assert plan.risk_reasons==['public_quote_stale_or_unproven']
        assert manager.place_entry_order_calls==0
        assert risk.release_calls
        assert all(asyncio.run(cash.repository.get_reservation(t)).status=='released' for t in risk.release_calls)
    else:
        assert plan.quantity==1 and plan.order_id=='OID123'
        assert risk.reserve_calls[-1]['quantity']==1
        assert risk.reserve_calls[-1]['entry_price']==(3.0 if change=='price' else 2.85)


@pytest.mark.parametrize('mode', ['urgent', 'balanced', 'price_seeking'])
def test_final_live_quote_age_gate_also_covers_initially_fresh_quotes(monkeypatch, mode):
    freeze(monkeypatch)
    dep = _enabled_deployment('market_impulse_qqq_short_v1')
    dep.execution.entry_pricing_mode = mode
    manager = StubOrderManager()
    manager.get_option_quote = AsyncMock(return_value=quote())
    async def preflight(symbol, price, quantity):
        from bhiksha.execution.order_manager import PreflightCheck
        Clock.current = NOW + timedelta(seconds=9)
        return PreflightCheck(payload={'limitPrice':str(price)},current_increment=.01,
                              buying_power_requirement=price*quantity*100,estimated_cost=None)
    manager.preflight_entry = preflight
    risk = StubRiskManager()
    cash = RecordingAllowedCashGuard()
    planner = ExecutionPlanner(chain_service=StubChainService(),order_manager=manager,
        cash_guard=cash,risk_manager=risk,position_tracker=PositionTracker())
    decision = SignalDecision(dep.deployment_id,'QQQ',NOW,True,SignalDirection.SHORT,[],{})
    plan = asyncio.run(planner.plan_entry(dep,decision,dry_run=False))
    assert plan.risk_reasons == ['public_quote_stale_or_unproven']
    assert manager.get_option_quote.await_count == (3 if mode == 'price_seeking' else 2)
    assert manager.place_entry_order_calls == 0
    assert risk.release_calls
    assert all(('release', trade_id) in cash.calls for trade_id in risk.release_calls)
    assert all(('release',trade_id) in cash.calls for trade_id in risk.release_calls)
