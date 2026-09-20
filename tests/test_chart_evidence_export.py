from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bhiksha.ops.chart_evidence_export import export_chart_evidence


DAY = "2026-09-01"


def _db(path: Path, *, complete: bool = False) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        conn.execute("""CREATE TABLE trade_sessions (
            trade_id TEXT, deployment_id TEXT, symbol TEXT, option_symbol TEXT,
            entry_timestamp TEXT, underlying_entry_price REAL, status TEXT, updated_at TEXT
        )""")
        deployment = {
            "deployment_id": "qqq-shadow-v1", "symbol": "QQQ", "enabled": True,
            "strategy": {"key": "market_impulse"},
            "execution": {"shadow_only": True},
        }
        events = [
            (1, "2026-09-01T14:00:00+00:00", "startup_config", {"deployments": [deployment]}),
            (2, "2026-09-01T14:31:00+00:00", "signal_decision", {"deployment_id": "qqq-shadow-v1", "symbol": "QQQ", "timestamp": "2026-09-01T14:30:00+00:00", "signal": True}),
            (3, "2026-09-01T14:32:00+00:00", "trade_plan", {"deployment_id": "qqq-shadow-v1", "trade_id": "trade-actual-7", "option_symbol": "QQQ260901C00500000", "payload": {"accountId": "must-not-export"}}),
            (4, "2026-09-01T14:33:00+00:00", "entry_fill_check", {"deployment_id": "qqq-shadow-v1", "trade_id": "trade-actual-7", "option_symbol": "QQQ260901C00500000", "status": "FILLED", "filledQuantity": 1, "underlying_entry_price": 501.25}),
        ]
        if complete:
            events.append((5, "2026-09-01T20:10:00+00:00", "session_complete", {"deployment_id": "qqq-shadow-v1", "complete": True}))
        conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", [(i, at, kind, json.dumps(payload)) for i, at, kind, payload in events])
        conn.execute("INSERT INTO trade_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("trade-actual-7", "qqq-shadow-v1", "QQQ", "QQQ260901C00500000", "2026-09-01T14:33:00+00:00", 501.25, "open_protected", "2026-09-01T14:34:00+00:00"))
    return path


def test_export_is_idempotent_and_separates_fill_from_order_attempt(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db")
    first = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY)
    second = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY)

    assert first.path.name == f"{first.packet['id']}.json"
    assert second.reused is True
    assert first.packet == second.packet
    assert first.packet["publication"]["coverage"][0]["status"] == "partial"
    assert first.packet["publication"]["coverage"][0]["domains"] == ["signal", "order", "fill", "management", "exit", "rejection"]
    assert first.packet["publication"]["coverage"][0]["caseId"] == first.packet["cases"][0]["id"]
    assert first.packet["cases"][0]["extensions"]["bhiksha:scope"] == {
        "deploymentId": "qqq-shadow-v1", "mode": "shadow", "strategy": "market_impulse"
    }
    domains = {record["label"]: record["domain"] for record in first.packet["records"]}
    assert domains["order attempt recorded"] == "order"
    assert domains["confirmed entry fill recorded"] == "fill"
    fill = next(record for record in first.packet["records"] if record["domain"] == "fill")
    assert fill["extensions"]["bhiksha:execution"]["tradeId"] == "trade-actual-7"
    assert fill["extensions"]["bhiksha:execution"]["optionRight"] == "call"
    assert "must-not-export" not in first.path.read_text(encoding="utf-8")
    assert "price" not in next(record for record in first.packet["records"] if record["label"] == "order attempt recorded")["visuals"][0]


def test_late_confirmed_fill_survives_unrelated_event_volume(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE events SET payload=? WHERE id=1",
            (json.dumps({"deployments": [{
                "deployment_id": "qqq-shadow-v1", "symbol": "QQQ", "enabled": True,
                "strategy": {"key": "market_impulse"},
                "execution": {"runtime_mode": "live_approval_gated"},
            }]}),),
        )
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            [
                (10_000 + index, "2026-09-01T14:40:00+00:00", "noisy_telemetry", "{}")
                for index in range(30_000)
            ],
        )
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            (50_100, "2026-09-01T15:00:00+00:00", "entry_fill_check", json.dumps({
                "deployment_id": "qqq-shadow-v1", "trade_id": "late-fill", "status": "FILLED", "filledQuantity": 1
            })),
        )

    packet = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY).packet

    assert any(
        record["domain"] == "fill"
        and record.get("extensions", {}).get("bhiksha:execution", {}).get("tradeId") == "late-fill"
        for record in packet["records"]
    )


def test_false_signal_has_no_chart_marker_and_terminal_live_trade_is_a_fill(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db")
    with sqlite3.connect(db) as conn:
        startup = {"deployments": [{
            "deployment_id": "spy-live-v1", "symbol": "SPY", "enabled": True,
            "strategy": {"key": "opening_drive"},
            "execution": {"runtime_mode": "live_approval_gated"},
        }]}
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            [
                (20, "2026-09-01T14:01:00+00:00", "startup_config", json.dumps(startup)),
                (21, "2026-09-01T14:02:00+00:00", "signal_evaluation", json.dumps({
                    "deployment_id": "spy-live-v1", "symbol": "SPY", "signal": False
                })),
            ],
        )
        conn.execute(
            "INSERT INTO trade_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("live-trade-1", "spy-live-v1", "SPY", "SPY260901C00500000", "2026-09-01T14:03:00+00:00", 500.0, "open_protected", "2026-09-01T14:04:00+00:00"),
        )

    packet = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY).packet
    false_signal = next(record for record in packet["records"] if record["label"] == "non-signal evaluation recorded")
    live_fill = next(record for record in packet["records"] if record["id"].startswith("bhiksha-trade-entry"))

    assert "visuals" not in false_signal
    assert false_signal["extensions"]["bhiksha:execution"]["signal"] is False
    assert live_fill["basis"] == "recorded"
    assert live_fill["extensions"]["bhiksha:trade"]["tradeId"] == "live-trade-1"


def test_source_completion_can_certify_complete_and_revision_changes_packet(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db", complete=True)
    first = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY)
    corrected = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY, revision=2)

    assert first.packet["publication"]["coverage"][0]["status"] == "complete"
    assert corrected.packet["publication"]["streamId"] == first.packet["publication"]["streamId"]
    assert corrected.packet["id"] != first.packet["id"]
    assert corrected.path.exists() and first.path.exists()


def test_same_stream_revision_rejects_changed_source_content(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db")
    first = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            (99, "2026-09-01T15:10:00+00:00", "signal_decision", json.dumps({
                "deployment_id": "qqq-shadow-v1", "symbol": "QQQ", "signal": True
            })),
        )

    with pytest.raises(ValueError, match="higher --revision"):
        export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY)
    correction = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY, revision=2)

    assert correction.packet["id"] != first.packet["id"]
    assert correction.packet["publication"]["revision"] == 2


def test_matching_trade_receipts_are_corroboration_not_duplicate_markers(tmp_path: Path) -> None:
    db = _db(tmp_path / "bhiksha.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE events SET payload=? WHERE id=1",
            (json.dumps({"deployments": [{
                "deployment_id": "qqq-shadow-v1", "symbol": "QQQ", "enabled": True,
                "strategy": {"key": "market_impulse"},
                "execution": {"runtime_mode": "live_approval_gated"},
            }]}),),
        )
        for column, sql_type in (
            ("entry_order_id", "TEXT"), ("exit_order_id", "TEXT"), ("exit_price", "REAL"),
            ("exit_filled_quantity", "INTEGER"), ("exit_filled_at", "TEXT"), ("exit_order_status", "TEXT"),
        ):
            conn.execute(f"ALTER TABLE trade_sessions ADD COLUMN {column} {sql_type}")
        conn.execute(
            "UPDATE trade_sessions SET entry_order_id=?, exit_order_id=?, exit_price=?, exit_filled_quantity=?, exit_filled_at=?, exit_order_status=?, status='closed'",
            ("entry-order-1", "exit-order-1", 0.8, 1, "2026-09-01T15:10:00+00:00", "FILLED"),
        )
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            [
                (101, "2026-09-01T14:34:00+00:00", "entry_fill_check", json.dumps({
                    "deployment_id": "qqq-shadow-v1", "order_id": "entry-order-1", "status": "FILLED", "filledQuantity": 1
                })),
                (102, "2026-09-01T15:11:00+00:00", "exit_fill_enriched", json.dumps({
                    "deployment_id": "qqq-shadow-v1", "trade_id": "trade-actual-7", "exit_order_id": "exit-order-1", "status": "FILLED"
                })),
            ],
        )

    packet = export_chart_evidence(db, output_dir=tmp_path / "out", trading_date=DAY).packet
    qqq_case = packet["cases"][0]["id"]
    exits = [record for record in packet["records"] if record["caseId"] == qqq_case and record["domain"] == "exit"]
    corroborations = [record for record in packet["records"] if record["label"].endswith("corroboration recorded")]

    assert sum(bool(record.get("visuals")) for record in exits) == 1
    assert {record["label"] for record in corroborations} >= {
        "entry fill corroboration recorded", "exit fill corroboration recorded"
    }
