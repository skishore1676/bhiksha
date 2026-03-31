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
