"""Operator input and family behavior regressions, independent of live services."""
from dataclasses import replace
from datetime import UTC, datetime, time
import asyncio
import sqlite3

import pytest

from bhiksha.active_plan.compiler import _exit_spec_fields_from_management_policy_spec
from bhiksha.config.exit_catalog import load_exit_profiles_sheet_rows
from bhiksha.execution.profile_exit import ProfileExitFields, ProfileExitState, ProfileMarketView, evaluate_profile_exit
from test_entry_exit_refactor import profile


def decision(config, *, peak_r, current_r):
    fields = ProfileExitFields.from_management_spec(config.to_management_policy_spec_dict())
    return evaluate_profile_exit(fields=fields, entry_premium=2, quantity=2,
        market=ProfileMarketView(current_premium=2 + .6 * current_r, bar_time_et=time(10)),
        entry_time=datetime(2026, 9, 18, 14, tzinfo=UTC), now=datetime(2026, 9, 18, 14, tzinfo=UTC),
        state=ProfileExitState(peak_premium=2 + .6 * peak_r))


def test_profit_lock_is_distinct_from_giveback_and_only_arms_from_prior_peak():
    config = profile('lock', exit_family='profit_preservation_ratchet', profit_lock_arm_r=.75, profit_lock_floor_r=.25)
    assert not decision(config, peak_r=.74, current_r=.2).exit
    result = decision(config, peak_r=.8, current_r=.2)
    assert result.exit and result.reason == 'profile_policy_floor'
    assert result.features['floor_r'] == .25
    assert not decision(profile(), peak_r=.8, current_r=.2).exit


def test_dynamic_floor_and_frozen_runtime_adapter_use_same_policy():
    from bhiksha.config.models import ExitSpec
    config = profile('envelope', exit_family='dynamic_envelope', risk_envelope_enabled=True,
        risk_envelope_activation_r=.5, risk_envelope_initial_floor_r=-1,
        risk_envelope_floor_at_t1_r=0, risk_envelope_curvature=1.5, risk_envelope_ratchet_step_r=.1)
    result = decision(config, peak_r=.8, current_r=-.7)
    assert result.exit and result.reason == 'profile_policy_floor'
    assert result.features['floor_r'] == pytest.approx(-.6)
    assert not decision(config, peak_r=.5, current_r=-.7).exit
    mapped = _exit_spec_fields_from_management_policy_spec(config.to_management_policy_spec_dict())
    spec = ExitSpec.model_validate({**mapped, 'management_exit': 'envelope'})
    runtime = ProfileExitFields.from_exit_spec(spec)
    replay = ProfileExitFields.from_management_spec(spec.exit_policy_snapshot)
    assert runtime.policy_floor == replay.policy_floor
    assert runtime.policy_hash == spec.exit_policy_hash


@pytest.mark.parametrize('key', ['breakeven_after_t1', 'eod_flat', 'risk_envelope_enabled'])
def test_bad_sheet_booleans_never_silently_disable_protection(key):
    with pytest.raises(ValueError, match='must be true or false'):
        load_exit_profiles_sheet_rows([{**profile().model_dump(), key: 'tru'}])


def test_missing_family_parameters_and_unsupported_anchor_fail_explicitly():
    for changes in ({'exit_family': 'profit_preservation_ratchet'},
                    {'exit_family': 'dynamic_envelope', 'risk_envelope_enabled': True, 'risk_envelope_curvature': 1.5},
                    {'stop_anchor': 'underlying_entry_bar_failure'}):
        with pytest.raises(ValueError):
            load_exit_profiles_sheet_rows([{**profile().model_dump(), **changes}])


def test_cartographer_reordered_sheet_named_exit_projection_compiles_and_preserves_operator_cells(tmp_path):
    from test_cartographer_projector import _Table, _batch, _operator_defaults
    from bhiksha.integrations.cartographer_projector import NAMED_EXIT_HEADERS, project_with_table, row_to_compiler_payload
    from bhiksha.active_plan.compiler import ActivePlanSheetRow, compile_active_plan_from_rows
    defaults = _operator_defaults()
    defaults['profile__trend_continuation'].update(management_exit='base', compare_exits='base,patient')
    headers = list(NAMED_EXIT_HEADERS)
    headers.remove('management_policy'); headers.insert(10, 'management_policy')
    table = _Table(headers, [])
    result = project_with_table(table, _batch(), operator_defaults=defaults, trading_date='2026-08-17', apply=True)
    assert result['sheet_write_outcome'] == 'confirmed'
    record = table.read_rows()[0]
    assert record['management_exit'] == 'base' and record['management_policy_spec'] == ''
    row = ActivePlanSheetRow.model_validate(row_to_compiler_payload([record.get(k, '') for k in NAMED_EXIT_HEADERS]))
    catalog = tmp_path/'catalog'; catalog.mkdir()
    compiled = compile_active_plan_from_rows(rows=[row], strategy_catalog_path=catalog, trading_date='2026-08-17',
        operator_defaults=defaults, exit_profiles_catalog={'base': profile('base'), 'patient': profile('patient', no_progress_seconds=180)})
    assert not compiled.plan.suppressed
    assert compiled.plan.deployments[0].exit.compare_exits == ['base', 'patient']
    defaults['profile__trend_continuation']['management_exit'] = 'changed'
    project_with_table(table, _batch(), operator_defaults=defaults, trading_date='2026-08-17', apply=True)
    assert table.read_rows()[0]['management_exit'] == 'base'


def test_rail_b_database_history_is_not_displaced_by_other_lanes(tmp_path):
    from bhiksha.domain.models import TradeRecord
    from bhiksha.persistence.sqlite import SQLiteTradeStateRepository
    async def run():
        repo = SQLiteTradeStateRepository(str(tmp_path/'trades.db'))
        for name, lane in [('old-live', 'live'), ('new-paper', 'paper')]:
            await repo.upsert_trade(TradeRecord(trade_id=name, deployment_id=lane, symbol='QQQ',
                option_symbol='OPTION', quantity=1, entry_price=2, status='closed', entry_order_id=name))
        rows = await repo.get_closed_trades_for_deployment('live')
        assert [r.trade_id for r in rows] == ['old-live']
    asyncio.run(run())


def test_old_floor_label_is_censored_not_reinterpreted_as_new_mechanics(tmp_path):
    from test_rail_b_recovery import sample
    from bhiksha.ops.exit_edge_lab import analyze_cases
    dep, cases = sample(tmp_path)
    old = dict(cases[0].experiment['named_profiles'][0])
    old['parameters'] = {**old['parameters'], 'exit_family':'profit_preservation_ratchet'}
    old['parameters'].pop('named_exit_evaluator_version')
    from bhiksha.ops.exit_edge_lab import experiment_spec_hash
    experiment = {**cases[0].experiment, 'named_profiles':[old]}
    case = replace(cases[0], experiment=experiment, experiment_spec_hash=experiment_spec_hash(cases[0].profile_config, cases[0].legacy_config, experiment))
    row = analyze_cases([case])['cases'][0]
    assert row['status'] == 'insufficient_data'
    assert row['insufficient_reason'] == 'unsupported_historical_named_exit_mechanics'
