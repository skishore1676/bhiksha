from __future__ import annotations

import json
import sqlite3

from bhiksha.ops.daily_report import _report_status
from bhiksha.ops.provider_reconciliation_health import (
    _RECENT_RECONCILIATION_SQL,
    inspect_provider_reconciliation,
    summarize_provider_reconciliation,
)


def _event(event_type: str, payload: dict, created_at: str) -> dict:
    return {"event_type": event_type, "payload": payload, "created_at": created_at}


def test_indexed_ledger_status_uses_bounded_rowid_tail(tmp_path) -> None:
    database = tmp_path / "events.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "CREATE INDEX idx_events_event_type_id ON events(event_type, id DESC)"
        )
        connection.executemany(
            "INSERT INTO events(created_at, event_type, payload) VALUES (?, ?, ?)",
            [
                ("2026-07-17T14:11:11+00:00", "reconciliation_health", json.dumps({"severity": "warning"})),
                ("2026-07-17T14:11:12+00:00", "runtime_metric", json.dumps({"metric": "signal_evaluation_ms"})),
                ("2026-07-17T14:11:26+00:00", "runtime_metric", json.dumps({"metric": "portfolio_sync_ms"})),
            ],
        )
        plan = connection.execute(
            "EXPLAIN QUERY PLAN " + _RECENT_RECONCILIATION_SQL,
            (
                "reconciliation_health",
                "reconciliation_recovered",
                "runtime_issue",
                "reconciliation",
                "runtime_metric",
                "portfolio_sync_ms",
                2,
            ),
        ).fetchall()
    assert any("SCAN events" in row[3] for row in plan)
    assert not any("USE TEMP B-TREE" in row[3] for row in plan)
    summary = inspect_provider_reconciliation(database, limit=2)
    assert summary["state"] == "recovered"
    assert summary["active_warning_count"] == 0


def test_success_after_warning_preserves_history_but_clears_attention() -> None:
    summary = summarize_provider_reconciliation(
        [
            _event(
                "reconciliation_health",
                {
                    "stage": "reconciliation",
                    "severity": "warning",
                    "recovery_state": "self_healing",
                    "attention_required": False,
                    "consecutive_failures": 1,
                    "error": "portfolio 400",
                },
                "2026-07-17T14:11:11+00:00",
            ),
            _event(
                "runtime_metric",
                {"metric": "portfolio_sync_ms", "value": 42.0},
                "2026-07-17T14:11:26+00:00",
            ),
        ]
    )

    assert summary["state"] == "recovered"
    assert summary["attention_required"] is False
    assert summary["warning_count"] == 1
    assert summary["active_warning_count"] == 0
    assert summary["recovered_count"] == 1
    assert _report_status(provider_events=summary, data_quality_warnings=[]) == {
        "level": "GREEN",
        "reason": "ok",
        "attention_required": False,
    }


def test_active_failure_stays_silent_inside_recovery_window() -> None:
    summary = summarize_provider_reconciliation(
        [
            _event(
                "reconciliation_health",
                {
                    "stage": "reconciliation",
                    "severity": "degraded",
                    "recovery_state": "self_healing",
                    "attention_required": False,
                    "consecutive_failures": 2,
                },
                "2026-07-17T14:11:26+00:00",
            )
        ]
    )

    assert summary["state"] == "self_healing"
    assert summary["attention_required"] is False
    assert summary["active_degraded_count"] == 1
    assert _report_status(provider_events=summary, data_quality_warnings=[]) == {
        "level": "YELLOW",
        "reason": "degraded_reconciliation",
        "attention_required": False,
    }


def test_unrelated_runtime_metric_cannot_false_clear_provider_failure() -> None:
    summary = summarize_provider_reconciliation(
        [
            _event(
                "reconciliation_health",
                {
                    "stage": "reconciliation",
                    "severity": "warning",
                    "attention_required": False,
                },
                "2026-07-17T14:11:11+00:00",
            ),
            _event(
                "runtime_metric",
                {"metric": "signal_evaluation_ms", "value": 2.0},
                "2026-07-17T14:11:12+00:00",
            ),
        ]
    )

    assert summary["state"] == "self_healing"
    assert summary["active_warning_count"] == 1
    assert summary["recovered_count"] == 0


def test_exhausted_recovery_requires_attention() -> None:
    summary = summarize_provider_reconciliation(
        [
            _event(
                "reconciliation_health",
                {
                    "stage": "reconciliation",
                    "severity": "degraded",
                    "recovery_state": "needs_human",
                    "attention_required": True,
                    "consecutive_failures": 21,
                    "failure_age_seconds": 305,
                },
                "2026-07-17T14:16:16+00:00",
            )
        ]
    )

    assert summary["state"] == "needs_human"
    assert summary["attention_required"] is True
    assert _report_status(provider_events=summary, data_quality_warnings=[]) == {
        "level": "RED",
        "reason": "reconciliation_recovery_exhausted",
        "attention_required": True,
    }
