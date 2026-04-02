from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from bhiksha.loop.observation import write_observation_reports
from bhiksha.persistence.sqlite import SQLiteEventRepository


def test_observation_report_summarizes_runtime_issues_and_blocked_reasons(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    generated_root = config_root / "deployments" / "generated"
    generated_root.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "sqlite_path": str(tmp_path / "events.db"),
                "observation_reports_dir": str(tmp_path / "reports"),
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
    (config_root / "bias_inputs.yaml").write_text("selections: []\n", encoding="utf-8")
    generated_manifest = {
        "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
        "enabled": True,
        "symbol": "QQQ",
        "strategy": {
            "key": "market_impulse",
            "version": 1,
            "params": {
                "direction": "short",
                "entry_buffer_minutes": 5,
                "entry_window_minutes": 60,
                "regime_timeframe": "1h",
                "vma_length": 10,
                "market_open_hour": 9,
                "market_open_minute": 30,
            },
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": True,
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
        "source": {
            "origin": "mala_loop_v1_1",
            "run_date": "2026-03-31",
            "artifact": "m5_execution_mapping.csv",
            "metadata": {
                "candidate_id": "market_candidate",
                "automation_lane": "automated_shadow",
                "bias_template": "bearish_trend_intraday",
                "horizon": "intraday",
            },
        },
    }
    (generated_root / "market_impulse_qqq_short_shadow_1234abcd.yaml").write_text(
        yaml.safe_dump(generated_manifest, sort_keys=False),
        encoding="utf-8",
    )

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    async def seed() -> None:
        await repo.append(
            "startup_config",
            {
                "config_fingerprint": "abc123fingerprint",
                "deployments": [{"deployment_id": "market_impulse_qqq_short_shadow_1234abcd"}],
            },
        )
        await repo.append(
            "signal_decision",
            {
                "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
                "symbol": "QQQ",
                "signal": True,
                "direction": "short",
                "reason": ["time_window_ok"],
            },
        )
        await repo.append(
            "trade_plan",
            {
                "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
                "symbol": "QQQ",
                "risk_reasons": ["public_spread_above_maximum"],
            },
        )
        await repo.append(
            "runtime_issue",
            {
                "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
                "symbol": "QQQ",
                "category": "data",
                "error": "quote unavailable",
            },
        )

    asyncio.run(seed())

    packets = asyncio.run(
        write_observation_reports(
            config_root=config_root,
            db_path=tmp_path / "events.db",
            output_dir=tmp_path / "reports",
            include_replay=False,
        )
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet["deployment_id"] == "market_impulse_qqq_short_shadow_1234abcd"
    assert packet["startup_config_fingerprint"] == "abc123fingerprint"
    assert packet["signal_true_count"] == 1
    assert packet["blocked_entry_reasons"]["public_spread_above_maximum"] == 1
    assert packet["runtime_issue_counts"]["data"] == 1
    assert packet["replay"]["status"] == "skipped"
    assert packet["safe_for_live_review"] is False

    written = json.loads((tmp_path / "reports" / "market_impulse_qqq_short_shadow_1234abcd.json").read_text(encoding="utf-8"))
    assert written["candidate_id"] == "market_candidate"


def test_observation_report_scopes_counts_to_latest_startup(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    generated_root = config_root / "deployments" / "generated"
    generated_root.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "sqlite_path": str(tmp_path / "events.db"),
                "observation_reports_dir": str(tmp_path / "reports"),
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
    (config_root / "bias_inputs.yaml").write_text("selections: []\n", encoding="utf-8")
    generated_manifest = {
        "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
        "enabled": True,
        "symbol": "QQQ",
        "strategy": {
            "key": "market_impulse",
            "version": 1,
            "params": {
                "direction": "short",
                "entry_buffer_minutes": 5,
                "entry_window_minutes": 60,
                "regime_timeframe": "1h",
                "vma_length": 10,
                "market_open_hour": 9,
                "market_open_minute": 30,
            },
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": True,
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
        "source": {
            "origin": "mala_loop_v1_1",
            "run_date": "2026-03-31",
            "artifact": "m5_execution_mapping.csv",
            "metadata": {
                "candidate_id": "market_candidate",
                "automation_lane": "automated_shadow",
                "bias_template": "bearish_trend_intraday",
                "horizon": "intraday",
            },
        },
    }
    (generated_root / "market_impulse_qqq_short_shadow_1234abcd.yaml").write_text(
        yaml.safe_dump(generated_manifest, sort_keys=False),
        encoding="utf-8",
    )

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    async def seed() -> None:
        await repo.append(
            "startup_config",
            {
                "config_fingerprint": "oldfingerprint",
                "deployments": [{"deployment_id": "market_impulse_qqq_short_shadow_1234abcd"}],
            },
        )
        await repo.append(
            "signal_decision",
            {
                "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
                "symbol": "QQQ",
                "signal": True,
                "direction": "short",
                "reason": ["time_window_ok"],
            },
        )
        await repo.append(
            "runtime_issue",
            {
                "deployment_id": "market_impulse_qqq_short_shadow_1234abcd",
                "symbol": "QQQ",
                "category": "data",
                "error": "quote unavailable",
            },
        )
        await repo.append(
            "startup_config",
            {
                "config_fingerprint": "newfingerprint",
                "deployments": [{"deployment_id": "market_impulse_qqq_short_shadow_1234abcd"}],
            },
        )

    asyncio.run(seed())

    packets = asyncio.run(
        write_observation_reports(
            config_root=config_root,
            db_path=tmp_path / "events.db",
            output_dir=tmp_path / "reports",
            include_replay=False,
        )
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet["startup_config_fingerprint"] == "newfingerprint"
    assert packet["signal_true_count"] == 0
    assert packet["runtime_issue_counts"] == {}
    assert packet["safe_for_live_review"] is False


def test_observation_report_supports_session_payload_manual_origin(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "sqlite_path": str(tmp_path / "events.db"),
                "observation_reports_dir": str(tmp_path / "reports"),
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
    (config_root / "bias_inputs.yaml").write_text("selections: []\n", encoding="utf-8")
    session_payload_path = tmp_path / "active_session.json"
    session_payload = {
        "contract_name": "active_session",
        "schema_version": 1,
        "session_id": "active_session_2026-04-02",
        "generated_at": "2026-04-02T12:00:00+00:00",
        "deployments": [
            {
                "deployment_id": "manual_trigger_spy_short_abc",
                "enabled": True,
                "symbol": "SPY",
                "strategy": {
                    "key": "manual_trigger",
                    "version": 1,
                    "params": {
                        "direction": "short",
                        "trigger_price": 530.0,
                        "trigger_direction": "BELOW",
                        "after_time_et": "8:35",
                    },
                },
                "execution": {
                    "profile": "single_leg_long_premium_v1",
                    "shadow_only": False,
                    "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                    "dte_min": 0,
                    "dte_max": 2,
                    "target_abs_delta_min": 0.45,
                    "target_abs_delta_max": 0.6,
                    "min_open_interest": 100,
                    "max_bid_ask_spread_pct": 0.2,
                },
                "risk": {
                    "profile": "manual_trigger_v1",
                    "max_trade_premium_usd": 300,
                    "hard_flat_time_et": "15:55",
                    "stop_loss_pct": 0.45,
                },
                "exit": {
                    "profile": "manual_trigger_exit_v1",
                    "use_algorithmic_exit": False,
                    "use_profit_target": True,
                    "profit_target_multiple": 1.5,
                    "stop_loss_pct": 0.45,
                    "hard_flat_time_et": "15:55",
                    "catastrophe_exit_anchor": "option_premium",
                    "catastrophe_exit_params": {"stop_loss_pct": 0.45, "hard_flat_time_et": "15:55"},
                },
                "source": {
                    "origin": "operator_manual",
                    "run_date": "2026-04-02",
                    "artifact": "entry_v1",
                    "metadata": {
                        "authorization_mode": "live",
                        "trade_id": "manual-1",
                    },
                },
            }
        ],
    }
    session_payload_path.write_text(json.dumps(session_payload, indent=2), encoding="utf-8")

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    async def seed() -> None:
        await repo.append(
            "startup_config",
            {
                "config_fingerprint": "livefingerprint",
                "deployment_selection": {
                    "mode": "session_payload",
                    "session_id": "active_session_2026-04-02",
                },
                "session": {"live": True, "max_bars": None},
                "deployments": session_payload["deployments"],
            },
        )
        await repo.append(
            "signal_decision",
            {
                "deployment_id": "manual_trigger_spy_short_abc",
                "symbol": "SPY",
                "signal": True,
                "direction": "short",
                "reason": ["manual_trigger_met"],
            },
        )
        await repo.append(
            "trade_plan",
            {
                "deployment_id": "manual_trigger_spy_short_abc",
                "symbol": "SPY",
                "risk_reasons": [],
            },
        )
        await repo.append(
            "exit_decision",
            {
                "deployment_id": "manual_trigger_spy_short_abc",
                "symbol": "SPY",
                "exit": True,
                "action": "square_off",
                "reason": ["profit_target_hit"],
            },
        )
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "manual_trigger_spy_short_abc",
                "symbol": "SPY",
                "previous_state": "pending_entry",
                "new_state": "closed",
                "reason": "exit_closed",
            },
        )

    asyncio.run(seed())

    packets = asyncio.run(
        write_observation_reports(
            config_root=config_root,
            db_path=tmp_path / "events.db",
            output_dir=tmp_path / "reports",
            include_replay=False,
            session_payload_path=session_payload_path,
        )
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet["source_origin"] == "operator_manual"
    assert packet["authorization_mode"] == "live"
    assert packet["session_mode"] == "session_payload"
    assert packet["session_id"] == "active_session_2026-04-02"
    assert packet["live_requested"] is True
    assert packet["shadow_only"] is False
    assert packet["signal_reason_counts"]["manual_trigger_met"] == 1
    assert packet["exit_reason_counts"]["profit_target_hit"] == 1
    assert packet["exit_true_count"] == 1
