import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.runtime import _preserve_newer_pending_exit_state
from bhiksha.config.models import ActivePlan, AppConfig
from bhiksha.execution.order_manager import OrderManager
from bhiksha.ops.daily_report import _app_running_row
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.tools.server_session import main as server_session_main
from test_midpoint_entry_contract import Broker, Clock, NOW, clock, lane, owners, signal


def named_lane():
    from bhiksha.active_plan.compiler import ActivePlanSheetRow, _apply_exit_overrides
    from bhiksha.config.models import ExitSpec
    from test_active_plan_compiler import _operator_exit_catalog
    dep = lane()
    row = ActivePlanSheetRow(row_id="named-shadow", row_type="manual", symbol="QQQ",
        manual_setup_type="manual_trigger", trigger_price=558, trigger_direction="BELOW",
        authorization_mode="shadow", direction="short", management_exit="trend_continuation_balanced",
        compare_exits=["trend_continuation_balanced", "flash_reversal_fast_snap"])
    dep.exit = ExitSpec.model_validate(_apply_exit_overrides(dep.exit.model_dump(), row,
        exit_catalog=_operator_exit_catalog()))
    return dep


def native_start(tmp_path, monkeypatch, *, enabled=True):
    flags = tmp_path / "artifacts/playbook/runtime_flags"
    flags.mkdir(parents=True)
    if enabled:
        (flags / "exit_edge_live_shadow.enabled").touch()
    captured = {}
    def popen(command, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(pid=424242)
    monkeypatch.setattr("bhiksha.tools.server_session.subprocess.Popen", popen)
    monkeypatch.setenv("BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED", str(not enabled).lower())
    monkeypatch.setenv("BHIKSHA_EXIT_EDGE_OBSERVER_EXTERNAL_ENABLED", str(not enabled).lower())
    assert server_session_main(["start", "--repo-root", str(tmp_path), "--pid-path", str(tmp_path / "owner.pid"),
        "--runtime-log-dir", str(tmp_path / "logs"), "--active-plan", str(tmp_path / "active_plan.json")]) == 0
    return captured


@pytest.mark.parametrize("enabled", [True, False])
def test_native_start_preserves_installer_owned_observation_mode(tmp_path, monkeypatch, enabled):
    env = native_start(tmp_path, monkeypatch, enabled=enabled)
    for key in ["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED", "BHIKSHA_EXIT_EDGE_OBSERVER_EXTERNAL_ENABLED"]:
        assert env[key] == str(enabled).lower()
    metadata = json.loads((tmp_path / "owner.pid").read_text())
    assert metadata["observation_flags"]["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED"] == str(enabled).lower()


def test_native_start_real_runtime_modeled_fill_registers_frozen_comparison(tmp_path, monkeypatch):
    from bhiksha.app import runtime as runtime_module
    env = native_start(tmp_path, monkeypatch)
    for key in ["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED", "BHIKSHA_EXIT_EDGE_OBSERVER_EXTERNAL_ENABLED"]:
        monkeypatch.setenv(key, env[key])
    config = tmp_path / "config"
    config.mkdir(); (config / "strategy_catalog").mkdir()
    edge = tmp_path / "edge.db"
    app = AppConfig(sqlite_path=str(tmp_path / "events.db"), strategy_catalog_dir="config/strategy_catalog",
        playbook_artifacts_dir=str(tmp_path / "artifacts"), deployment_selection_mode="manual_only",
        exit_edge_live_shadow_enabled=False, exit_edge_live_shadow_db_path=str(edge),
        exit_edge_live_shadow_status_path=str(tmp_path / "edge_status.json"))
    (config / "app.yaml").write_text(yaml.safe_dump(app.model_dump()))
    (config / "providers.yaml").write_text(yaml.safe_dump({"underlying_live_primary":"public", "underlying_backfill_primary":"public"}))
    dep = named_lane()
    dep.execution = dep.execution.model_copy(update={"shadow_only":True})
    plan_path = tmp_path / "active_plan.json"
    plan_path.write_text(ActivePlan(active_plan_id="registration-bootstrap", deployments=[dep]).model_dump_json())
    runtime = build_runtime(config_root=config, active_plan_path=plan_path)
    assert runtime.app_config.exit_edge_live_shadow_enabled
    broker = Broker()
    monkeypatch.setattr(runtime_module, "OrderManager", lambda **kwargs: OrderManager(broker=broker))
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_build_manual_status_writer", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_live_bar_source", lambda self: SimpleNamespace(close=AsyncMock()))
    captured = {}
    async def reconcile(self, *, supervisor, **kwargs):
        captured["supervisor"] = supervisor
    async def warm(self, *args, **kwargs):
        from test_execution_planner import StubChainService
        supervisor = captured["supervisor"]
        assert supervisor.exit_edge_recorder.role == "registration"
        supervisor.planner.chain_service = StubChainService()
        pending = await supervisor.handle_signal(dep, signal(dep), dry_run=True, simulate_only=True)
        captured["trade_id"] = pending.trade_id
        assert not supervisor.planner.position_tracker.active_positions()
        Clock.current += timedelta(seconds=1); broker.ask = pending.estimated_entry_price
        await supervisor.poll_paper_entries(now=Clock.current)
        return []
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "_refresh_reconciliation", reconcile)
    monkeypatch.setattr(runtime_module.BhikshaRuntime, "warm_start_symbol", warm)
    asyncio.run(runtime.run_session(live=True, max_bars=0, output=lambda line: None))
    assert not broker.buy_orders
    repository = ProspectiveQuoteTapeRepository(edge, read_only=True)
    case = repository.load_case("exit-edge:" + captured["trade_id"])
    assert case.quantity > 0
    assert case.cohort_dimensions["entry_fill_kind"] == "modeled_ask_touch"
    with sqlite3.connect(app.sqlite_path) as c:
        facts = [json.loads(p) for (p,) in c.execute("select payload from events where event_type='shadow_entry_modeled'")]
    filled = facts[0]
    assert filled["exit_edge_registration"]["cohort"]["trade_id"] == captured["trade_id"]


def test_named_shadow_management_persists_stop_across_ticks_and_recovery(tmp_path):
    async def run():
        broker = Broker(); planner, supervisor, trades = owners(tmp_path, broker)
        dep = named_lane()
        pending = await supervisor.handle_signal(dep, signal(dep), dry_run=True, simulate_only=True)
        Clock.current += timedelta(seconds=1); broker.ask = pending.estimated_entry_price
        await supervisor.poll_paper_entries(now=Clock.current)
        for _ in range(2):
            current = planner.position_tracker.active_positions()[0]
            await supervisor.manage_open_position(dep, current, dry_run=False)
        assert planner.position_tracker.active_positions()[0].stop_order_id == "DRY_RUN_STOP"
        assert (await trades.get_open_trades())[0].stop_order_id == "DRY_RUN_STOP"
        planner.position_tracker.replace_positions([])
        await supervisor.sync_lifecycle()
        await supervisor.manage_open_position(dep, planner.position_tracker.active_positions()[0], dry_run=False)
        with sqlite3.connect(tmp_path / "trades.db") as c:
            assert c.execute("select count(*) from events where event_type='protection_restore_attempt'").fetchone()[0] == 1
        assert not broker.buy_orders
    asyncio.run(run())


def test_reconciliation_preserves_current_paper_protection_and_close():
    stale = TrackedPosition("QQQ", "shadow", trade_id="paper", option_symbol="QQQ261016P00558000", source="shadow")
    protected = replace(stale, stop_order_id="DRY_RUN_STOP", stop_price=1.5)
    assert _preserve_newer_pending_exit_state([stale], [protected]) == [protected]
    assert _preserve_newer_pending_exit_state([stale], []) == []
    assert _preserve_newer_pending_exit_state([], [protected]) == [protected]


@pytest.mark.parametrize("receipt,green", [
    ({"recorded_at":"2026-10-08T20:10:00+00:00", "status":"ok", "detail":"stopped"}, True),
    ({"recorded_at":"2026-10-08T18:00:00+00:00", "status":"ok", "detail":"stopped"}, False),
    ({"recorded_at":"2026-10-07T20:10:00+00:00", "status":"ok", "detail":"stopped"}, False),
    ({"recorded_at":"2026-10-08T20:10:00+00:00", "status":"ok", "detail":"not_running"}, False),
])
def test_stopped_app_requires_current_successful_close_receipt(receipt, green):
    row = _app_running_row({"trading_date":"2026-10-08"}, app_status={"running":False,
        "checked_at":"2026-10-08T20:15:00+00:00", "session_stop_receipt":receipt})
    assert (row[2] == "🟢") is green
    if not green:
        assert row[2] == "🔴"
