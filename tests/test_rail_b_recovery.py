import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bhiksha.risk.rail_b_recovery import RailBRecoveryPolicy, assess_sample, recovery_candidate
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository, QuoteTapeMark
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder, QUOTE_FEED, QUOTE_SOURCE
from test_entry_exit_refactor import deployment


def policy():
    return RailBRecoveryPolicy(min_shadow_trades=20, min_sessions=5, max_age_days=14,
        min_mean_net_r=.1, round_trip_cost_per_contract_usd=1, premium_cap_fraction=.2)


def sample(tmp_path):
    dep = deployment()
    dep.exit.compare_exit_policies = dep.exit.compare_exit_policies[:1]
    dep.exit.compare_exits = dep.exit.compare_exits[:1]
    at = datetime(2026, 9, 14, 14, tzinfo=UTC)
    recorder = ExitEdgeLiveRecorder(db_path=tmp_path/'tape.db', status_path=tmp_path/'status.json')
    _, payload = recorder._registration_payloads(deployment=dep, trade_id='seed', option_symbol='QQQ260918P00500000',
        entry_timestamp=at, entry_premium=2, quantity=2, entry_context={'entry_fill_kind':'modeled_ask_touch'})
    repo = ProspectiveQuoteTapeRepository(tmp_path/'tape.db'); repo.initialize(); repo.register_cohort(payload)
    for seq in [1, 2]:
        moment = at + timedelta(seconds=60+seq)
        repo.append_quote(payload['cohort_id'], QuoteTapeMark(seq, QUOTE_SOURCE, QUOTE_FEED, moment, moment, 2.1, 2.15))
    case = repo.load_case(payload['cohort_id'])
    cases=[]
    for n in range(20):
        shift=timedelta(days=n//4,minutes=n%4*5)
        cases.append(replace(case, trade_id=str(n), cohort_id=str(n), cluster_id=str(n//4),
            entry_timestamp=at+shift, quotes=tuple(replace(q, quote_at=q.quote_at+shift,received_at=q.received_at+shift) for q in case.quotes)))
    return dep,cases


def test_recovery_requires_fresh_complete_cost_adjusted_multi_session_sample(tmp_path):
    dep,cases=sample(tmp_path)
    kwargs=dict(deployment_id=dep.deployment_id,policy_hash=dep.exit.exit_policy_hash,
                since=datetime(2026,9,13,tzinfo=UTC),now=datetime(2026,9,19,tzinfo=UTC),policy=policy())
    assert assess_sample(cases,**kwargs)['eligible']
    assert not assess_sample(cases[:19],**kwargs)['eligible']
    assert not assess_sample(cases,**{**kwargs,'policy_hash':'changed'})['eligible']
    assert not assess_sample(cases,**{**kwargs,'since':datetime(2026,9,18,21,tzinfo=UTC)})['eligible']
    assert not assess_sample(cases,**{**kwargs,'policy':policy().model_copy(update={'round_trip_cost_per_contract_usd':15})})['eligible']
    damaged=[replace(c,persisted_censor_reason='missing_quote') for c in cases]
    assert not assess_sample(damaged,**kwargs)['eligible']
    assumed=[replace(c,cohort_dimensions={**c.cohort_dimensions,'entry_fill_kind':'assumed'}) for c in cases]
    assert not assess_sample(assumed,**kwargs)['eligible']


def test_recovery_never_promotes_research_shadow_or_changes_source_cap(tmp_path,monkeypatch):
    dep,cases=sample(tmp_path)
    dep.execution.rail_b_recovery=policy()
    sink=SimpleNamespace(append=AsyncMock())
    manager=SimpleNamespace(trade_state_repository=SimpleNamespace(
        get_closed_trades_for_deployment=AsyncMock(return_value=[SimpleNamespace(entry_order_id='live',exit_filled_at=datetime(2026,9,13,tzinfo=UTC))]),
        get_open_trades=AsyncMock(return_value=[])),settings=SimpleNamespace(demote_reset_at=None))
    kwargs=dict(deployment=dep,risk_manager=manager,db_path=tmp_path/'tape.db',event_repository=sink)
    assert asyncio.run(recovery_candidate(**kwargs)) is None
    manager.trade_state_repository.get_closed_trades_for_deployment.assert_not_called()
    dep.execution.shadow_only=False;dep.execution.runtime_mode='live_approval_gated'
    dep.risk.max_trade_premium_usd=1000
    monkeypatch.setattr('bhiksha.risk.rail_b_recovery._read_cases',lambda *args:cases)
    monkeypatch.setattr('bhiksha.risk.rail_b_recovery.assess_sample',lambda *args,**kwargs:{'eligible':True})
    candidate=asyncio.run(recovery_candidate(**kwargs))
    assert candidate.risk.max_trade_premium_usd==200 and candidate.risk.max_contracts==1
    assert dep.risk.max_trade_premium_usd==1000
    manager.trade_state_repository.get_open_trades.return_value=[SimpleNamespace(deployment_id=dep.deployment_id,entry_order_id='live')]
    assert asyncio.run(recovery_candidate(**kwargs)) is None
