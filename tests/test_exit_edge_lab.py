from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    QuoteTapeMark,
    analyze_cases,
    build_historical_coverage_report,
    load_fixture_cases,
    policy_config_hash,
)

ENTRY = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)


def _raw_case() -> dict:
    profile = {
        "profile_exit_id": "profile__trend_continuation", "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.35, "target_1_r": 1.0, "target_2_r": 2.0,
        "target_1_quantity": 0.6, "high_water_giveback_policy": "OFF",
        "eod_flat": True, "hard_flat_time_et": "15:55",
    }
    legacy = {"stop_loss_pct": 0.35, "profit_target_pct": 0.35, "hard_flat_time_et": "15:55"}
    bids = [2.10, 2.70, 2.75, 3.40, 3.35]
    return {
        "cohort_id": "C1", "trade_id": "T1", "deployment_id": "qqq-live",
        "symbol": "QQQ", "option_symbol": "OPT", "entry_timestamp": ENTRY.isoformat(),
        "entry_premium": 2.0, "quantity": 10, "profile": profile, "legacy": legacy,
        "policy_config_hash": policy_config_hash(profile, legacy),
        "quotes": [{
            "sequence": i, "quote_at": (ENTRY+timedelta(seconds=i*15)).isoformat(),
            "source": "public_cache",
            "received_at": (ENTRY+timedelta(seconds=i*15, milliseconds=100)).isoformat(),
            "bid": bid, "ask": bid+0.05, "last": bid+0.02,
        } for i,bid in enumerate(bids, start=1)],
    }


def _load(tmp_path: Path, raw: dict):
    path=tmp_path/"fixture.json"; path.write_text(json.dumps({"cases":[raw]}))
    return load_fixture_cases(path)


def test_next_tick_bid_fill_pairs_partial_and_runner(tmp_path: Path) -> None:
    row=analyze_cases(_load(tmp_path,_raw_case()))["cases"][0]
    assert row["status"] == "paired"
    # Both trigger at seq2. Legacy fills all 10 at seq3 bid 2.75 (+750).
    assert row["legacy_outcome"]["legs"][0]["trigger_sequence"] == 2
    assert row["legacy_outcome"]["legs"][0]["fill_sequence"] == 3
    assert row["legacy_outcome"]["realized_pnl_usd"] == 750.0
    # Profile banks 6 at seq3 (+450), triggers T2 seq4, fills 4 seq5 (+540).
    assert row["profile_outcome"]["realized_pnl_usd"] == 990.0
    assert row["paired_delta_pnl_usd"] == 240.0
    assert row["profile_outcome"]["legs"][1]["fill_sequence"] == 5


def test_wide_quote_still_uses_executable_bid_not_midpoint(tmp_path: Path) -> None:
    raw=_raw_case(); raw["quotes"][2]["ask"]=4.75
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["status"] == "paired"
    assert row["legacy_outcome"]["realized_pnl_usd"] == 750.0


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r["quotes"][0].update(bid=None), "missing_executable_bid"),
    (lambda r: r["quotes"][0].update(ask=2.0), "crossed_quote"),
    (lambda r: r["quotes"][1].update(sequence=1), "duplicate_or_out_of_order_sequence"),
    (lambda r: r["quotes"][1].update(sequence=3), "sequence_gap"),
])
def test_bad_quote_paths_are_censored(tmp_path: Path, mutation, reason: str) -> None:
    raw=_raw_case(); mutation(raw)
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["status"] == "insufficient_data"
    assert row["insufficient_reason"] == reason


def test_stale_quote_and_policy_mutation_are_censored(tmp_path: Path) -> None:
    raw=_raw_case(); raw["quotes"][0]["received_at"]=(ENTRY+timedelta(seconds=30)).isoformat()
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "stale_quote_gap"
    raw=_raw_case(); raw["profile"]["target_2_r"]=3.0
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "policy_config_hash_mismatch"


def test_tape_end_before_second_arm_fill_is_right_censored(tmp_path: Path) -> None:
    raw=_raw_case(); raw["quotes"]=raw["quotes"][:4]
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["status"] == "insufficient_data"
    assert row["insufficient_reason"] == "right_censored:profile"


def test_repository_is_idempotent_and_conflicts_do_not_overwrite(tmp_path: Path) -> None:
    repo=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); repo.initialize(); raw=_raw_case()
    repo.register_cohort(raw); repo.register_cohort(raw)
    q=QuoteTapeMark(1,"public_cache",ENTRY+timedelta(seconds=1),ENTRY+timedelta(seconds=1,milliseconds=10),2.0,2.1,2.05)
    repo.append_quote("C1",q)
    # A new process/repository instance can replay the same append after restart.
    restarted=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); restarted.append_quote("C1",q)
    with pytest.raises(ValueError):
        repo.append_quote("C1",QuoteTapeMark(1,"public_cache",q.quote_at,q.received_at,1.9,2.1,2.0))
    changed=dict(raw); changed["quantity"]=11
    with pytest.raises(ValueError): repo.register_cohort(changed)
    with sqlite3.connect(tmp_path/"lab.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM exit_edge_quote_tape").fetchone()[0] == 1


def test_storage_failure_is_failure_isolated_for_live_caller(tmp_path: Path) -> None:
    # A directory path cannot be opened as SQLite. Best-effort recording returns
    # False; the caller's already-computed live decision remains untouched.
    repo=ProspectiveQuoteTapeRepository(tmp_path)
    live_decision={"action":"square_off","timestamp":ENTRY.isoformat()}
    assert repo.try_initialize() is False
    assert repo.try_register_cohort(_raw_case()) is False
    assert live_decision == {"action":"square_off","timestamp":ENTRY.isoformat()}


def test_historical_mode_reports_coverage_only(tmp_path: Path) -> None:
    db=tmp_path/"snapshot.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE trade_sessions (trade_id TEXT,deployment_id TEXT,option_symbol TEXT,
          entry_timestamp TEXT,exit_filled_at TEXT,status TEXT);
        CREATE TABLE events (id INTEGER,created_at TEXT,event_type TEXT,payload TEXT);
        """)
        exit_at=ENTRY+timedelta(minutes=1)
        conn.execute("INSERT INTO trade_sessions VALUES (?,?,?,?,?,?)",("T1","D","OPT",ENTRY.isoformat(),exit_at.isoformat(),"closed"))
        conn.execute("INSERT INTO events VALUES (?,?,?,?)",(1,(exit_at+timedelta(seconds=1)).isoformat(),"shadow_mark",json.dumps({"trade_id":"T1"})))
        conn.execute("INSERT INTO events VALUES (?,?,?,?)",(2,exit_at.isoformat(),"profile_exit_shadow",json.dumps({"current_premium":2.0})))
    before=db.read_bytes(); report=build_historical_coverage_report(db,start="2026-07-09",end="2026-07-09")
    assert db.read_bytes() == before
    assert report["verdict"] == "historical_data_ineligible_for_paired_outcome_estimation"
    assert report["counts"]["trades_with_any_post_exit_mark"] == 1
    assert report["counts"]["eligible_paired_trades"] == 0
