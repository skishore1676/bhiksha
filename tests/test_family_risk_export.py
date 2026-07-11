from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bhiksha.tools.family_risk_export import atomic_write_json, build_export


def _db(path: Path, *, status: str = "open_protected", option_symbol: str = "SPY260717C00750000") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE trade_sessions (
            trade_id TEXT, deployment_id TEXT, symbol TEXT, option_symbol TEXT,
            quantity INTEGER, entry_price REAL, stop_price REAL, status TEXT,
            entry_order_id TEXT, updated_at TEXT)""")
        conn.execute("INSERT INTO trade_sessions VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "trade-1", "dep-1", "SPY", option_symbol, 2, 1.5, .9, status,
            "raw-broker-order-secretish", "2026-07-10T19:59:00+00:00"))
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        conn.execute("INSERT INTO events VALUES (1,?,?,?)", (
            "2026-07-10T19:58:30+00:00", "runtime_metric", json.dumps({"metric": "portfolio_sync_ms"})))


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
    assert row["contract_multiplier"] is None
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


def test_atomic_writer_leaves_no_partial_file_on_replace_failure(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "snapshot.json"
    out.write_text('{"old": true}\n')
    monkeypatch.setattr("bhiksha.tools.family_risk_export.os.replace",
                        lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        atomic_write_json(out, {"new": True})
    assert json.loads(out.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))
