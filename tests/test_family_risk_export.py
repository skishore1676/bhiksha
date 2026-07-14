from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bhiksha.tools.family_risk_export import atomic_write_json, build_export


def _db(
    path: Path,
    *,
    status: str = "open_protected",
    option_symbol: str = "SPY260717C00750000",
    symbol: str = "SPY",
    entry_price: float | None = 1.5,
    stop_price: float | None = 0.9,
    with_budget: bool = True,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE trade_sessions (
            trade_id TEXT, deployment_id TEXT, symbol TEXT, option_symbol TEXT,
            quantity INTEGER, entry_price REAL, stop_price REAL, status TEXT,
            entry_order_id TEXT, updated_at TEXT)""")
        conn.execute("INSERT INTO trade_sessions VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "trade-1", "dep-1", symbol, option_symbol, 2, entry_price, stop_price, status,
            "raw-broker-order-secretish", "2026-07-10T19:59:00+00:00"))
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        conn.execute("INSERT INTO events VALUES (1,?,?,?)", (
            "2026-07-10T19:58:30+00:00", "runtime_metric", json.dumps({"metric": "portfolio_sync_ms"})))
        if with_budget:
            conn.execute("""CREATE TABLE cash_budget_days (
                trade_date TEXT PRIMARY KEY, account_type TEXT,
                broker_cash_only_buying_power REAL NOT NULL, usable_budget REAL NOT NULL,
                buffer_pct REAL NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO cash_budget_days VALUES (?,?,?,?,?,?)", (
                "2026-07-10", "CASH", 12000.0, 11400.0, 0.05, "2026-07-10T13:45:00+00:00"))


def test_export_is_allowlisted_hashed_and_non_gating(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "b.db"; _db(db)
    cache = tmp_path / "account.json"
    cache.write_text(json.dumps({"accountId": "raw-account-id", "secret": "do-not-copy"}))
    monkeypatch.setenv("FAMILY_RISK_IDENTITY_HMAC_KEY", "operator-test-key")
    payload = build_export(db_path=db, account_cache=cache, account_alias="bhiksha-public",
                           now=datetime(2026, 7, 10, 20, 0, tzinfo=UTC))
    encoded = json.dumps(payload)
    assert payload["source_observed_at"] == "2026-07-10T19:58:30+00:00"
    assert payload["account_identity_verified"] is True
    assert "raw-account-id" not in encoded and "do-not-copy" not in encoded
    assert "raw-broker-order-secretish" not in encoded
    row = payload["trade_sessions"][0]
    assert row["contract_multiplier"] == 100
    assert row["broker_position_ids"][0].startswith("public-option:sha256:")
    assert "gate" not in payload and "enforcement" not in payload


def test_closed_rows_are_excluded_but_observation_is_not_freshened(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db, status="closed")
    payload = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x",
                           now=datetime(2026, 7, 11, tzinfo=UTC))
    assert payload["trade_sessions"] == []
    assert payload["source_observed_at"] == "2026-07-10T19:58:30+00:00"
    assert payload["generated_at"] != payload["source_observed_at"]


def test_ambiguous_open_instrument_refuses_entire_export(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db, option_symbol="SPY-CALL")
    with pytest.raises(ValueError, match="unambiguous OCC"):
        build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")


@pytest.mark.parametrize("status", ["protection_failed_exit_pending", "critical_unprotected"])
def test_high_risk_packet_lifecycle_states_remain_visible(tmp_path: Path, status: str) -> None:
    db = tmp_path / "b.db"; _db(db, status=status)
    payload = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")
    assert [row["status"] for row in payload["trade_sessions"]] == [status]


def test_account_summary_reports_assigned_capital_without_broker_call(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db)
    payload = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x",
                           now=datetime(2026, 7, 10, 20, 0, tzinfo=UTC))
    summary = payload["account_summary"]
    assert summary["assigned_capital"] == 11400.0
    assert summary["assigned_capital_basis"] == "cash_guard_usable_daily_budget"
    assert summary["broker_cash_only_buying_power"] == 12000.0
    assert summary["usable_daily_budget"] == 11400.0
    assert summary["budget_buffer_pct"] == 0.05
    assert summary["account_type"] == "CASH"
    assert summary["observed_at"] == "2026-07-10T13:45:00+00:00"
    # No family-wide buying-power sum is produced by the exporter.
    assert "buying_power_total" not in json.dumps(payload)


def test_missing_cash_budget_leaves_summary_null_with_explicit_gap(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db, with_budget=False)
    payload = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")
    assert payload["account_summary"] is None
    assert any("cash_budget_days" in gap for gap in payload["adapter_gaps"])


def test_long_option_defined_risk_is_calculated(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db)  # entry 1.5, stop 0.9, qty 2, mult 100
    payload = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")
    row = payload["trade_sessions"][0]
    assert row["multiplier_provenance"] == "app_standard_option_contract"
    assert row["multiplier_provenance_detail"].startswith("occ_standard_equity_option")
    assert row["long_option_capital"] == 300.0  # 1.5 * 2 * 100
    assert row["worst_case_loss"] == 300.0  # long option max loss == full premium
    assert row["planned_stop_loss"] == 120.0  # (1.5 - 0.9) * 2 * 100
    assert row["risk_notes"] == []


def test_missing_stop_declines_planned_loss_but_keeps_worst_case(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db, stop_price=None)
    row = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")["trade_sessions"][0]
    assert row["worst_case_loss"] == 300.0
    assert row["planned_stop_loss"] is None
    assert any("stop_price not persisted" in note for note in row["risk_notes"])


def test_missing_entry_declines_all_risk_without_guessing(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db, entry_price=None)
    row = build_export(db_path=db, account_cache=tmp_path / "missing", account_alias="x")["trade_sessions"][0]
    assert row["long_option_capital"] is None
    assert row["worst_case_loss"] is None
    assert row["planned_stop_loss"] is None
    assert any("entry_price not persisted" in note for note in row["risk_notes"])


def test_cluster_is_mapped_when_supported_and_null_otherwise(tmp_path: Path) -> None:
    known = tmp_path / "known.db"
    _db(known, symbol="NVDA", option_symbol="NVDA260717C00750000")
    row = build_export(db_path=known, account_cache=tmp_path / "m", account_alias="x")["trade_sessions"][0]
    assert row["cluster"] == "semiconductors"
    assert row["cluster_provenance"] == "app_static_underlying_cluster_map"

    unknown = tmp_path / "unknown.db"
    _db(unknown, symbol="ZZZZ", option_symbol="ZZZZ260717C00750000")
    payload = build_export(db_path=unknown, account_cache=tmp_path / "m", account_alias="x")
    urow = payload["trade_sessions"][0]
    assert urow["cluster"] is None and urow["cluster_provenance"] is None
    assert any("ZZZZ" in gap for gap in payload["adapter_gaps"])


def test_broker_observation_exposes_staleness_seconds(tmp_path: Path) -> None:
    db = tmp_path / "b.db"; _db(db)
    payload = build_export(db_path=db, account_cache=tmp_path / "m", account_alias="x",
                           now=datetime(2026, 7, 10, 20, 0, tzinfo=UTC))
    obs = payload["broker_observation"]
    assert obs["source_observed_at"] == "2026-07-10T19:58:30+00:00"
    assert obs["staleness_seconds"] == 90.0  # 20:00:00 - 19:58:30


def test_atomic_writer_leaves_no_partial_file_on_replace_failure(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "snapshot.json"
    out.write_text('{"old": true}\n')
    monkeypatch.setattr("bhiksha.tools.family_risk_export.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        atomic_write_json(out, {"new": True})
    assert json.loads(out.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))
