import asyncio
import json
import sqlite3
from datetime import UTC, datetime

from bhiksha.app.runtime import record_signal_evaluation
from bhiksha.domain.models import SignalDecision
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
                "previous_state": "open_protected",
                "new_state": "exit_pending",
                "reason": "exit_submitted",
            },
        )
        await repo.append(
            "ambiguous_cancel",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "order_id": "STOP123",
                "kind": "stop",
                "reason": "strategy_exit",
            },
        )
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "previous_state": "exit_pending",
                "new_state": "open_protected",
                "reason": "broker_reconciliation_sync",
            },
        )

    asyncio.run(seed())

    summary = build_session_summary(str(db_path), recent_limit=5)

    assert summary.total_events == 7
    assert summary.event_type_counts["lifecycle_transition"] == 3
    assert summary.deployment_event_counts["market_impulse_qqq_short_v1"] == 7
    assert summary.signal_true_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.exit_true_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.pending_exit_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.ambiguous_cancel_counts["market_impulse_qqq_short_v1"] == 1
    assert summary.lifecycle_last_state["market_impulse_qqq_short_v1"] == "open_protected"
    assert summary.recent_events[-1].detail == "exit_pending->open_protected (broker_reconciliation_sync)"
    recent_details = [event.detail for event in summary.recent_events]
    assert "signal=True direction=short reasons=time_window_ok" in recent_details
    assert "exit=True action=square_off reasons=vma_reclaim_exit" in recent_details


def test_record_signal_evaluation_persists_false_decision_without_signal_decision_count(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))
    decision = SignalDecision(
        deployment_id="market_impulse_qqq_short_v1",
        symbol="QQQ",
        timestamp=datetime(2026, 5, 1, 14, 35, tzinfo=UTC),
        signal=False,
        reason=["volume_gate_blocked"],
        features={"volume": 100.0},
    )

    asyncio.run(record_signal_evaluation(repo, decision))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()

    assert [row[0] for row in rows] == ["signal_evaluation"]
    payload = json.loads(rows[0][1])
    assert payload["signal"] is False
    assert payload["reason"] == ["volume_gate_blocked"]

    summary = build_session_summary(str(db_path), recent_limit=5)
    assert summary.event_type_counts["signal_evaluation"] == 1
    assert summary.signal_true_counts == {}


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
                    "mode": "active_plan",
                    "active_plan_id": "active_plan_2026-04-02",
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
    assert summary.latest_startup_snapshot["deployment_selection"]["mode"] == "active_plan"
    assert summary.latest_startup_snapshot["session"]["live"] is True
