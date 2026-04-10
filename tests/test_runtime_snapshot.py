import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import yaml

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.runtime import ReconciliationSnapshot
from bhiksha.state.position_tracker import PositionTracker


def test_runtime_startup_snapshot_includes_fingerprint_and_enabled_deployments() -> None:
    runtime = build_runtime()

    snapshot = runtime.startup_snapshot(live=False, max_bars=5)

    assert "config_fingerprint" in snapshot
    assert len(snapshot["config_fingerprint"]) == 16
    assert snapshot["session"] == {"live": False, "max_bars": 5}
    assert snapshot["app"]["app_name"] == "bhiksha"
    assert snapshot["providers"]["execution_broker_primary"] == "public"
    assert {entry["strategy_id"] for entry in snapshot["strategy_catalog"]} >= {
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }
    assert {selection["symbol"] for selection in snapshot["bias_inputs"]} >= {"IWM", "TSLA"}
    assert snapshot["emergency_controls"] == {"halt_and_flatten": False}
    assert snapshot["deployment_selection"]["mode"] == "prefer_generated"
    assert {deployment["deployment_id"] for deployment in snapshot["deployments"]} >= {
        "market_impulse_spy_short_armed_a98a25d2",
        "market_impulse_qqq_short_v1",
    }


def test_build_runtime_respects_configured_bias_inputs_path(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    deployments_root.mkdir(parents=True)
    bias_inputs_path = tmp_path / "research" / "bias_inputs.yaml"
    bias_inputs_path.parent.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "bias_inputs_path": "research/bias_inputs.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "underlying_live_primary": "polygon",
                "underlying_backfill_primary": "polygon",
                "execution_broker_primary": "public",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    bias_inputs_path.write_text(
        yaml.safe_dump(
            {
                "selections": [
                    {
                        "symbol": "QQQ",
                        "bias_template": "bearish_trend_intraday",
                        "horizon": "intraday",
                        "enabled": True,
                        "max_active_candidates": 1,
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    runtime = build_runtime(config_root)

    assert [selection.symbol for selection in runtime.bias_inputs] == ["QQQ"]


def test_runtime_reload_bias_controls_updates_intraday_emergency_flag(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    deployments_root.mkdir(parents=True)
    bias_inputs_path = tmp_path / "research" / "bias_inputs.yaml"
    bias_inputs_path.parent.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "bias_inputs_path": "research/bias_inputs.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "underlying_live_primary": "polygon",
                "underlying_backfill_primary": "polygon",
                "execution_broker_primary": "public",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    bias_inputs_path.write_text(
        yaml.safe_dump(
            {
                "emergency": {"halt_and_flatten": False},
                "selections": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    runtime = build_runtime(config_root)
    assert runtime.halt_and_flatten is False

    bias_inputs_path.write_text(
        yaml.safe_dump(
            {
                "emergency": {"halt_and_flatten": True},
                "selections": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    class StubRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def append(self, event_type: str, payload: dict) -> None:
            self.events.append((event_type, payload))

    class StubSupervisor:
        def __init__(self) -> None:
            self.event_repository = StubRepo()

    changed = asyncio.run(
        runtime._refresh_intraday_bias_controls(
            supervisor=StubSupervisor(),
            output=lambda _: None,
        )
    )

    assert changed is True
    assert runtime.halt_and_flatten is True


def test_build_runtime_prefers_generated_deployments_for_same_symbol(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    generated_root = deployments_root / "generated"
    generated_root.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "deployment_selection_mode": "prefer_generated",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "underlying_live_primary": "polygon",
                "underlying_backfill_primary": "polygon",
                "execution_broker_primary": "public",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_manifest(deployments_root / "manual_spy.yaml", "manual_spy", symbol="SPY")
    _write_manifest(generated_root / "generated_spy.yaml", "generated_spy", symbol="SPY")
    _write_manifest(deployments_root / "manual_qqq.yaml", "manual_qqq", symbol="QQQ")

    runtime = build_runtime(config_root)

    assert {deployment.deployment_id for deployment in runtime.deployments} == {"generated_spy", "manual_qqq"}
    skipped_ids = {entry["deployment_id"] for entry in runtime.deployment_selection["skipped"]}
    assert skipped_ids == {"manual_spy"}


def test_build_runtime_uses_active_plan_as_sole_authority(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    deployments_root.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump({"app_name": "bhiksha"}, sort_keys=False),
        encoding="utf-8",
    )
    (config_root / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "underlying_live_primary": "polygon",
                "underlying_backfill_primary": "polygon",
                "execution_broker_primary": "public",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_manifest(deployments_root / "manual_spy.yaml", "manual_spy", symbol="SPY")
    active_plan_path = tmp_path / "active_plan.json"
    active_plan_path.write_text(
        json.dumps(
            {
                "contract_name": "active_plan",
                "schema_version": 1,
                "active_plan_id": "active_plan_2026-04-01",
                "generated_at": "2026-04-01T12:00:00+00:00",
                "deployments": [
                    {
                        **_manifest_payload("session_iwm", symbol="IWM"),
                        "strategy": {
                            "key": "manual_trigger",
                            "version": 1,
                            "params": {
                                "direction": "long",
                                "trigger_price": 210.0,
                                "trigger_direction": "ABOVE",
                            },
                        },
                        "exit": {
                            **_manifest_payload("session_iwm", symbol="IWM")["exit"],
                            "use_algorithmic_exit": False,
                        },
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    runtime = build_runtime(config_root, active_plan_path=active_plan_path)

    assert [deployment.deployment_id for deployment in runtime.deployments] == ["session_iwm"]
    assert runtime.deployment_selection["mode"] == "active_plan"
    assert runtime.active_plan is not None
    assert runtime.active_plan["active_plan_id"] == "active_plan_2026-04-01"


def test_runtime_refresh_reconciliation_retries_timeout_and_recovers() -> None:
    runtime = build_runtime()
    snapshot = ReconciliationSnapshot()

    class StubBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def get_portfolio(self) -> dict:
            self.calls += 1
            if self.calls < 3:
                raise httpx.TimeoutException("timed out")
            return {
                "positions": [
                    {
                        "instrument": {
                            "symbol": "QQQ260401P00556000",
                            "type": "OPTION",
                        },
                        "quantity": "1.0",
                    }
                ],
                "orders": [],
            }

    class StubRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def append(self, event_type: str, payload: dict) -> None:
            self.events.append((event_type, payload))

    class StubTradeStateRepository:
        async def get_open_trades(self) -> list:
            return []

    class StubSupervisor:
        def __init__(self) -> None:
            self.event_repository = StubRepo()
            self.trade_state_repository = StubTradeStateRepository()
            self.planner = type("Planner", (), {"position_tracker": PositionTracker()})()
            self.sync_calls = 0

        async def sync_lifecycle(self) -> None:
            self.sync_calls += 1

    broker = StubBroker()
    supervisor = StubSupervisor()

    asyncio.run(
        runtime._refresh_reconciliation(
            broker=broker,
            supervisor=supervisor,
            sync_lock=asyncio.Lock(),
            reconciliation_snapshot=snapshot,
            output=lambda _: None,
            reason="periodic",
        )
    )

    assert broker.calls == 3
    assert snapshot.last_success_at is not None
    assert snapshot.consecutive_failures == 0
    assert snapshot.last_error is None
    assert supervisor.sync_calls == 1
    assert len(snapshot.positions) == 1


def test_runtime_reconciliation_staleness_blocks_live_entries() -> None:
    runtime = build_runtime()
    snapshot = ReconciliationSnapshot(
        last_attempt_at=datetime.now(UTC),
        last_success_at=datetime.now(UTC) - timedelta(seconds=61),
        consecutive_failures=2,
    )

    reason = runtime._reconciliation_live_entry_block_reason(snapshot, now=datetime.now(UTC))

    assert reason == "reconciliation_too_stale"


def _write_manifest(path: Path, deployment_id: str, *, symbol: str) -> None:
    payload = _manifest_payload(deployment_id, symbol=symbol)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _manifest_payload(deployment_id: str, *, symbol: str) -> dict:
    return {
        "deployment_id": deployment_id,
        "enabled": True,
        "symbol": symbol,
        "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": 7,
            "target_abs_delta_min": 0.2,
            "target_abs_delta_max": 0.4,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.2,
        },
        "risk": {
            "profile": "conservative_day1",
            "max_trade_premium_usd": 300,
            "hard_flat_time_et": "15:55",
            "stop_loss_pct": 0.45,
        },
        "exit": {
            "profile": "market_impulse_exit_v1",
            "use_algorithmic_exit": True,
            "use_profit_target": False,
            "profit_target_multiple": None,
            "stop_loss_pct": 0.45,
            "stop_to_breakeven_after_r_multiple": None,
            "hard_flat_time_et": "15:55",
        },
        "source": {"origin": "test", "run_date": "2026-04-01", "artifact": "test.csv"},
    }
