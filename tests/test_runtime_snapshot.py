import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import polars as pl
import yaml

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.runtime import ReconciliationSnapshot, _frame_with_live_price
from bhiksha.domain.models import Bar
from bhiksha.state.position_tracker import PositionTracker, TrackedPosition


def test_runtime_startup_snapshot_includes_fingerprint_and_enabled_deployments() -> None:
    runtime = build_runtime()

    snapshot = runtime.startup_snapshot(live=False, max_bars=5)

    assert "config_fingerprint" in snapshot
    assert len(snapshot["config_fingerprint"]) == 16
    assert snapshot["session"] == {"live": False, "max_bars": 5}
    assert snapshot["app"]["app_name"] == "bhiksha"
    assert snapshot["providers"]["underlying_live_primary"] == "schwab"
    assert snapshot["providers"]["underlying_backfill_primary"] == "schwab"
    assert snapshot["providers"]["execution_broker_primary"] == "public"
    assert {entry["strategy_id"] for entry in snapshot["strategy_catalog"]}.isdisjoint(
        {
            "market_impulse_qqq_short_v1",
            "market_impulse_spy_short_v1",
        }
    )
    assert {selection["symbol"] for selection in snapshot["bias_inputs"]} >= {"IWM", "TSLA"}
    assert snapshot["emergency_controls"] == {"halt_and_flatten": False}
    assert snapshot["deployment_selection"]["mode"] == "prefer_generated"
    deployment_ids = {deployment["deployment_id"] for deployment in snapshot["deployments"]}
    assert "market_impulse_qqq_short_v1" not in deployment_ids
    assert "market_impulse_spy_short_v1" not in deployment_ids
    assert snapshot["warmup"]["policy"] == "feature_contract_v1"
    assert snapshot["warmup"]["legacy_effective_trading_days"] == 5
    assert snapshot["warmup"]["effective_trading_days"] >= 5
    assert snapshot["warmup"]["by_symbol"] == {}


def test_runtime_warmup_expands_for_hourly_market_impulse() -> None:
    runtime = build_runtime()
    runtime.deployments[0].enabled = True
    runtime.deployments[0].symbol = "MU"
    runtime.deployments[0].strategy.key = "market_impulse"
    runtime.deployments[0].strategy.params = {
        "regime_timeframe": "1h",
        "vwma_periods": [10, 20, 40],
    }

    assert runtime.warmup_trading_days_for_symbol("MU") == 9


def test_warm_start_symbol_defaults_to_backfill_provider(monkeypatch) -> None:
    runtime = build_runtime()
    runtime.provider_config.underlying_live_primary = "public"
    runtime.provider_config.underlying_backfill_primary = "schwab"
    calls: list[str] = []

    class StubSchwabBarSource:
        async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
            calls.append(f"schwab:{symbol}")
            return [
                Bar(
                    symbol=symbol,
                    timestamp=datetime(2026, 5, 21, 14, 30, tzinfo=UTC),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1000.0,
                )
            ]

        async def close(self) -> None:
            calls.append("schwab:close")

    class ExplodingPublicBarSource:
        async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
            raise AssertionError("warm_start_symbol must not default to live Public")

        async def close(self) -> None:
            return None

    monkeypatch.setattr("bhiksha.app.runtime.SchwabBarSource", StubSchwabBarSource)
    monkeypatch.setattr("bhiksha.app.runtime.PublicBarSource", ExplodingPublicBarSource)

    bars = asyncio.run(runtime.warm_start_symbol("QQQ", warmup_trading_days=1))

    assert [bar.symbol for bar in bars] == ["QQQ"]
    assert calls == ["schwab:QQQ", "schwab:close"]


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


def test_manual_intrabar_loop_records_fetch_errors_without_crashing() -> None:
    runtime = build_runtime()
    runtime.app_config.bar_poll_interval_seconds = 1
    stop_event = asyncio.Event()
    recorded: list[tuple[str, dict]] = []
    output_lines: list[str] = []

    class FailingSource:
        async def fetch_live_price(self, symbol: str):
            raise RuntimeError(f"{symbol} quote failed")

    class StubRepo:
        async def append(self, event_type: str, payload: dict) -> None:
            recorded.append((event_type, payload))

    class StubSupervisor:
        event_repository = StubRepo()

    def output(line: str) -> None:
        output_lines.append(line)
        if line.startswith("RUNTIME_ISSUE"):
            stop_event.set()

    async def run() -> None:
        await asyncio.wait_for(
            runtime._manual_intrabar_loop(
                source=FailingSource(),
                store=None,
                supervisor=StubSupervisor(),
                evaluator=None,
                execution_dispatcher=None,
                deployments_by_symbol={"QQQ": [object()]},
                reconciliation_snapshot=ReconciliationSnapshot(),
                sync_lock=asyncio.Lock(),
                reconcile_trigger=asyncio.Event(),
                stop_event=stop_event,
                live=False,
                output=output,
            ),
            timeout=2,
        )

    asyncio.run(run())

    assert recorded == [
        (
            "runtime_issue",
            {
                "category": "data",
                "symbol": "QQQ",
                "error": "QQQ quote failed",
                "stage": "manual_intrabar",
            },
        )
    ]
    assert output_lines == ["RUNTIME_ISSUE QQQ stage=manual_intrabar error=QQQ quote failed"]


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
    deployment = next(deployment for deployment in runtime.deployments if deployment.symbol == "SPY")
    deployment.enabled = True
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
                                "symbol": "SPY260401P00556000",
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

        async def get_recent_trades(self, *, limit: int = 100) -> list:
            del limit
            return []

    class StubSupervisor:
        def __init__(self) -> None:
            self.event_repository = StubRepo()
            self.trade_state_repository = StubTradeStateRepository()
            self.planner = type("Planner", (), {"position_tracker": PositionTracker()})()
            self.sync_calls = 0

        async def sync_lifecycle(self) -> None:
            self.sync_calls += 1

        async def manage_open_position(self, deployment, position, *, dry_run: bool):
            del deployment, position, dry_run
            return None

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


def test_runtime_reconciliation_single_periodic_failure_is_warning_only() -> None:
    runtime = build_runtime()
    snapshot = ReconciliationSnapshot(last_success_at=datetime.now(UTC))
    output_lines: list[str] = []

    class StubBroker:
        async def get_portfolio(self) -> dict:
            raise httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("GET", "https://example.test/portfolio"),
                response=httpx.Response(400, request=httpx.Request("GET", "https://example.test/portfolio")),
            )

    class StubRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def append(self, event_type: str, payload: dict) -> None:
            self.events.append((event_type, payload))

    class StubTradeStateRepository:
        async def get_recent_trades(self, *, limit: int = 100) -> list:
            del limit
            return []

    class StubSupervisor:
        def __init__(self) -> None:
            self.event_repository = StubRepo()
            self.trade_state_repository = StubTradeStateRepository()
            self.planner = type("Planner", (), {"position_tracker": PositionTracker()})()

        async def sync_lifecycle(self) -> None:
            return None

        async def manage_open_position(self, deployment, position, *, dry_run: bool):
            del deployment, position, dry_run
            return None

    supervisor = StubSupervisor()

    asyncio.run(
        runtime._refresh_reconciliation(
            broker=StubBroker(),
            supervisor=supervisor,
            sync_lock=asyncio.Lock(),
            reconciliation_snapshot=snapshot,
            output=output_lines.append,
            reason="periodic",
        )
    )

    assert [event_type for event_type, _ in supervisor.event_repository.events] == ["reconciliation_health"]
    assert supervisor.event_repository.events[0][1]["severity"] == "warning"
    assert output_lines[0].startswith("RECONCILIATION_WARNING ")


def test_runtime_reconciliation_failure_with_live_position_is_blocking_runtime_issue() -> None:
    runtime = build_runtime()
    snapshot = ReconciliationSnapshot(
        positions=[
            TrackedPosition(
                symbol="IWM",
                deployment_id="iwm_live",
                option_symbol="IWM260609C00296000",
                quantity=1,
                source="broker_sync",
            )
        ],
        last_success_at=datetime.now(UTC) - timedelta(seconds=120),
        consecutive_failures=2,
    )
    output_lines: list[str] = []

    class StubBroker:
        async def get_portfolio(self) -> dict:
            raise TimeoutError("portfolio timed out")

    class StubRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def append(self, event_type: str, payload: dict) -> None:
            self.events.append((event_type, payload))

    class StubTradeStateRepository:
        async def get_recent_trades(self, *, limit: int = 100) -> list:
            del limit
            return []

    class StubSupervisor:
        def __init__(self) -> None:
            self.event_repository = StubRepo()
            self.trade_state_repository = StubTradeStateRepository()
            self.planner = type("Planner", (), {"position_tracker": PositionTracker()})()

        async def sync_lifecycle(self) -> None:
            return None

        async def manage_open_position(self, deployment, position, *, dry_run: bool):
            del deployment, position, dry_run
            return None

    supervisor = StubSupervisor()

    asyncio.run(
        runtime._refresh_reconciliation(
            broker=StubBroker(),
            supervisor=supervisor,
            sync_lock=asyncio.Lock(),
            reconciliation_snapshot=snapshot,
            output=output_lines.append,
            reason="periodic",
        )
    )

    assert [event_type for event_type, _ in supervisor.event_repository.events] == [
        "reconciliation_health",
        "runtime_issue",
    ]
    assert supervisor.event_repository.events[-1][1]["severity"] == "blocking"
    assert output_lines[0].startswith("RECONCILIATION_BLOCKING ")


def test_frame_with_live_price_appends_synthetic_quote_row() -> None:
    frame = _frame_with_live_price(
        "AAPL",
        [
            Bar(
                symbol="AAPL",
                timestamp=datetime(2026, 4, 10, 14, 30, tzinfo=UTC),
                open=259.0,
                high=259.2,
                low=258.9,
                close=259.1,
                volume=1000.0,
            )
        ],
        timestamp=datetime(2026, 4, 10, 14, 30, 15, tzinfo=UTC),
        price=259.5,
    )

    assert isinstance(frame, pl.DataFrame)
    assert frame.height == 2
    latest = frame.tail(1).to_dicts()[0]
    assert latest["timestamp"] == datetime(2026, 4, 10, 14, 30, 15, tzinfo=UTC)
    assert latest["close"] == 259.5
    assert latest["volume"] == 0.0


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
