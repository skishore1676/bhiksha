import asyncio

from bhiksha.ops.summary import build_session_summary
from bhiksha.persistence.sqlite import SQLiteEventRepository


def test_session_summary_aggregates_lifecycle_and_trade_events(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    async def seed():
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "previous_state": None,
                "new_state": "pending_entry",
                "reason": "entry_submitted",
            },
        )
        await repo.append(
            "trade_plan",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "option_symbol": "QQQ260401P00556000",
            },
        )
        await repo.append(
            "signal_decision",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "signal": True,
                "direction": "short",
                "reason": ["time_window_ok"],
            },
        )
        await repo.append(
            "exit_decision",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "exit": True,
                "action": "square_off",
                "reason": ["vma_reclaim_exit"],
            },
        )
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "previous_state": "pending_entry",
                "new_state": "open_protected",
                "reason": "entry_filled_open_protected",
            },
        )

    asyncio.run(seed())

    summary = build_session_summary(str(db_path), recent_limit=5)

    assert summary.total_events == 5
    assert summary.event_type_counts["lifecycle_transition"] == 2
    assert summary.deployment_event_counts["market_impulse_qqq_short_v1"] == 5
    assert summary.signal_true_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.exit_true_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.lifecycle_last_state["market_impulse_qqq_short_v1"] == "open_protected"
    assert summary.recent_events[-1].detail == "pending_entry->open_protected (entry_filled_open_protected)"
    assert summary.recent_events[2].detail == "signal=True direction=short reasons=time_window_ok"
    assert summary.recent_events[3].detail == "exit=True action=square_off reasons=vma_reclaim_exit"


def test_session_summary_aggregates_runtime_metrics(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    async def seed():
        await repo.append(
            "runtime_metric",
            {
                "metric": "heartbeat_lag_ms",
                "symbol": "QQQ",
                "value": 120.0,
                "unit": "ms",
            },
        )
        await repo.append(
            "runtime_metric",
            {
                "metric": "heartbeat_lag_ms",
                "symbol": "QQQ",
                "value": 180.0,
                "unit": "ms",
            },
        )
        await repo.append(
            "runtime_metric",
            {
                "metric": "execution_run_ms",
                "symbol": "QQQ",
                "action": "manage",
                "value": 45.5,
                "unit": "ms",
            },
        )
        await repo.append(
            "runtime_issue",
            {
                "category": "order",
                "symbol": "QQQ",
                "action": "entry",
                "error": "preflight failed",
            },
        )

    asyncio.run(seed())

    summary = build_session_summary(str(db_path), recent_limit=3)

    assert summary.runtime_metric_latest["heartbeat_lag_ms:QQQ"] == 180.0
    assert summary.runtime_metric_average["heartbeat_lag_ms:QQQ"] == 150.0
    assert summary.runtime_metric_latest["execution_run_ms:QQQ:manage"] == 45.5
    assert summary.runtime_issue_counts["order"] == 1


def test_session_summary_keeps_latest_startup_snapshot(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    async def seed():
        await repo.append(
            "startup_config",
            {
                "config_fingerprint": "fingerprint123",
                "deployment_selection": {
                    "mode": "session_payload",
                    "session_id": "active_session_2026-04-02",
                },
                "session": {"live": True, "max_bars": None},
                "deployments": [
                    {
                        "deployment_id": "manual_trigger_spy_short_abc",
                        "symbol": "SPY",
                        "strategy": {"key": "manual_trigger"},
                        "execution": {"shadow_only": False},
                        "source": {
                            "origin": "operator_manual",
                            "metadata": {
                                "authorization_mode": "live",
                                "trade_id": "manual-1",
                            },
                        },
                    }
                ],
            },
        )

    asyncio.run(seed())

    summary = build_session_summary(str(db_path), recent_limit=3)

    assert summary.latest_startup_created_at is not None
    assert summary.latest_startup_snapshot["deployment_selection"]["mode"] == "session_payload"
    assert summary.latest_startup_snapshot["session"]["live"] is True
