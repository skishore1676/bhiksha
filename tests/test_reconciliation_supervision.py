from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.alerts import AlertResult
from bhiksha.ops.daily_report import build_daily_report, render_daily_report_telegram_summary
from bhiksha.ops.reconciliation_supervision import (
    inspect_reconciliation_state,
    run_reconciliation_supervisor,
)
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.tools import launchd_job


NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)


def _seed_hold(db_path, *, updated_at: datetime, trade_id: str = "hold-amd") -> None:
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)
    events = SQLiteEventRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id=trade_id,
                deployment_id="amd_short_live",
                symbol="AMD",
                option_symbol="AMD260717P00150000",
                quantity=2,
                entry_price=8.60,
                entry_timestamp=updated_at,
                status="pending_entry_reconcile",
                entry_order_id="PUBLIC-AMD-ORDER",
            )
        )
        await events.append(
            "entry_fill_timeout_reconcile",
            {
                "trade_id": trade_id,
                "deployment_id": "amd_short_live",
                "symbol": "AMD",
                "entry_order_id": "PUBLIC-AMD-ORDER",
            },
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trade_sessions SET updated_at = ? WHERE trade_id = ?", (updated_at.isoformat(), trade_id))
        conn.execute(
            "UPDATE events SET created_at = ? WHERE event_type = 'entry_fill_timeout_reconcile'",
            (updated_at.isoformat(),),
        )
        conn.commit()


def test_inspection_keeps_transient_hold_self_healing_and_ages_stale_hold(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _seed_hold(db_path, updated_at=NOW - timedelta(seconds=45))

    transient = inspect_reconciliation_state(db_path, now=NOW)
    stale = inspect_reconciliation_state(db_path, now=NOW + timedelta(minutes=6))

    assert transient["state"] == "self_healing"
    assert transient["attention_required"] is False
    assert transient["active_holds"][0]["age_seconds"] == 45
    assert stale["state"] == "needs_human"
    assert stale["attention_required"] is True
    assert stale["active_holds"][0]["blocked_scope"] == "deployment"


def test_inspection_ages_hold_from_original_timeout_event_after_trade_refresh(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    started_at = NOW - timedelta(minutes=10)
    _seed_hold(db_path, updated_at=started_at)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_sessions SET updated_at = ? WHERE trade_id = 'hold-amd'",
            ((NOW - timedelta(seconds=20)).isoformat(),),
        )
        conn.commit()

    summary = inspect_reconciliation_state(db_path, now=NOW)

    assert summary["state"] == "needs_human"
    assert summary["active_holds"][0]["started_at"] == started_at.isoformat()
    assert summary["active_holds"][0]["age_seconds"] == 600


def test_supervisor_alerts_once_then_sends_one_recovery(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    receipt_dir = tmp_path / "receipts"
    _seed_hold(db_path, updated_at=NOW - timedelta(minutes=10))
    sent: list[dict] = []

    def fake_alert_sender(**kwargs):
        sent.append(kwargs)
        return AlertResult(
            attempted=True,
            ok=True,
            mode=kwargs["mode"],
            return_code=0,
            network_call_performed=True,
        )

    first = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW,
        alert_sender=fake_alert_sender,
    )
    duplicate = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW + timedelta(minutes=1),
        alert_sender=fake_alert_sender,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_sessions SET status = 'closed', updated_at = ?",
            ((NOW + timedelta(minutes=2)).isoformat(),),
        )
        conn.commit()
    recovered = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW + timedelta(minutes=2),
        alert_sender=fake_alert_sender,
    )

    assert first["alert_reason"] == "new_attention_state"
    assert duplicate["alert_reason"] == "duplicate_suppressed"
    assert recovered["alert_reason"] == "attention_cleared"
    assert recovered["alert_open"] is False
    assert len(sent) == 2
    assert "needs help" in sent[0]["title"]
    assert "recovered" in sent[1]["title"]
    assert (receipt_dir / "latest.json").is_file()
    assert len((receipt_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_supervisor_pages_exhausted_provider_recovery_then_clears(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    receipt_dir = tmp_path / "receipts"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)

    async def seed_failure() -> None:
        await events.append(
            "reconciliation_health",
            {
                "stage": "reconciliation",
                "severity": "degraded",
                "recovery_state": "needs_human",
                "attention_required": True,
                "consecutive_failures": 21,
                "failure_age_seconds": 305,
                "error": "portfolio unavailable",
            },
        )

    asyncio.run(seed_failure())
    sent: list[dict] = []

    def fake_alert_sender(**kwargs):
        sent.append(kwargs)
        return AlertResult(
            attempted=True,
            ok=True,
            mode=kwargs["mode"],
            return_code=0,
            network_call_performed=True,
        )

    first = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW,
        alert_sender=fake_alert_sender,
    )

    async def seed_recovery() -> None:
        await events.append("runtime_metric", {"metric": "portfolio_sync_ms", "value": 31.0})

    asyncio.run(seed_recovery())
    recovered = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW + timedelta(minutes=1),
        alert_sender=fake_alert_sender,
    )

    assert first["provider_reconciliation"]["state"] == "needs_human"
    assert first["alert_reason"] == "new_attention_state"
    assert "portfolio reconciliation" in sent[0]["body"].lower()
    assert recovered["provider_reconciliation"]["state"] == "recovered"
    assert recovered["attention_required"] is False
    assert recovered["alert_reason"] == "attention_cleared"
    assert len(sent) == 2


def test_partial_recovery_does_not_realert_surviving_hold(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    receipt_dir = tmp_path / "receipts"
    _seed_hold(db_path, updated_at=NOW - timedelta(minutes=10), trade_id="hold-amd")
    _seed_hold(db_path, updated_at=NOW - timedelta(minutes=10), trade_id="hold-nvda")
    sent: list[dict] = []

    def fake_alert_sender(**kwargs):
        sent.append(kwargs)
        return AlertResult(attempted=True, ok=True, mode="live", return_code=0, network_call_performed=True)

    first = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW,
        alert_sender=fake_alert_sender,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trade_sessions SET status = 'closed' WHERE trade_id = 'hold-amd'")
        conn.commit()
    second = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW + timedelta(minutes=1),
        alert_sender=fake_alert_sender,
    )

    assert first["needs_human_count"] == 2
    assert second["needs_human_count"] == 1
    assert second["alert_reason"] == "duplicate_suppressed"
    assert len(sent) == 1


def test_failed_attention_delivery_is_retried(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    receipt_dir = tmp_path / "receipts"
    _seed_hold(db_path, updated_at=NOW - timedelta(minutes=10))
    attempts = 0

    def flaky_alert_sender(**kwargs):
        nonlocal attempts
        attempts += 1
        return AlertResult(
            attempted=True,
            ok=attempts > 1,
            mode="live",
            return_code=0 if attempts > 1 else 1,
            network_call_performed=attempts > 1,
        )

    failed = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW,
        alert_sender=flaky_alert_sender,
    )
    retried = run_reconciliation_supervisor(
        db_path,
        receipt_dir=receipt_dir,
        alert_mode="live",
        now=NOW + timedelta(minutes=1),
        alert_sender=flaky_alert_sender,
    )

    assert failed["alert"]["ok"] is False
    assert failed["alert_open"] is False
    assert failed["alerted_attention_keys"] == []
    assert retried["alert"]["ok"] is True
    assert retried["alerted_attention_keys"]
    assert attempts == 2


def test_daily_report_does_not_call_entry_hold_an_open_position_or_trade(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _seed_hold(db_path, updated_at=NOW - timedelta(minutes=10))

    report = build_daily_report(db_path, trading_date="2026-07-16", now=NOW)
    telegram = render_daily_report_telegram_summary(report)

    assert report["trade_summary"]["live_count"] == 0
    assert report["trade_summary"]["live_open_count"] == 0
    assert report["open_positions"] == []
    assert report["trades"] == []
    assert report["entry_reconciliation"]["needs_human_count"] == 1
    assert report["status"] == {
        "level": "RED",
        "reason": "stale_entry_reconciliation_hold",
        "attention_required": True,
    }
    assert "Open positions\n- None" in telegram
    assert "NEEDS YOU - unresolved entry reconciliation: AMD" in telegram
    assert "unprotected" not in telegram.split("Open positions", 1)[1]


def test_daily_report_treats_released_zero_fill_as_recovery_not_trade(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _seed_hold(db_path, updated_at=NOW - timedelta(seconds=30))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE trade_sessions SET status = 'closed', updated_at = ?", (NOW.isoformat(),))
        conn.execute(
            "INSERT INTO events (created_at, event_type, payload) VALUES (?, ?, ?)",
            (
                NOW.isoformat(),
                "entry_reconcile_released",
                json.dumps(
                    {
                        "trade_id": "hold-amd",
                        "deployment_id": "amd_short_live",
                        "symbol": "AMD",
                        "entry_order_id": "PUBLIC-AMD-ORDER",
                        "status": "CANCELLED",
                    }
                ),
            ),
        )
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-07-16", now=NOW)

    assert report["trade_summary"]["live_count"] == 0
    assert report["trade_summary"]["live_missing_exit_truth_count"] == 0
    assert report["trades"] == []
    assert report["entry_reconciliation"]["recovered_count"] == 1
    assert report["entry_reconciliation"]["recoveries"][0]["action"] == "released_no_fill"
    assert report["status"] == {"level": "GREEN", "reason": "ok", "attention_required": False}


def test_launchd_supervisor_exposes_alert_transport_at_top_level(monkeypatch, tmp_path) -> None:
    app_config = SimpleNamespace(
        sqlite_path=str(tmp_path / "bhiksha.db"),
        playbook_artifacts_dir=str(tmp_path / "artifacts"),
    )
    monkeypatch.setattr(launchd_job, "build_runtime", lambda active_plan_path: SimpleNamespace(app_config=app_config))
    monkeypatch.setattr(
        launchd_job,
        "run_reconciliation_supervisor",
        lambda *args, **kwargs: {
            "job_status": "attention_required",
            "attention_required": True,
            "alert": {
                "attempted": True,
                "ok": False,
                "mode": "live",
                "network_call_performed": False,
            },
        },
    )
    printed: list[dict] = []
    monkeypatch.setattr(launchd_job, "_print_result", printed.append)
    args = SimpleNamespace(
        active_plan="active_plan.json",
        alert_mode="live",
        alert_profile="jarvis-northstar",
        job="reconciliation-supervisor",
    )

    return_code = launchd_job._reconciliation_supervisor_job(args, repo_root=tmp_path)

    assert return_code == 2
    assert printed[0]["alert"]["attempted"] is True
    assert printed[0]["alert"] == printed[0]["reconciliation_supervision"]["alert"]
