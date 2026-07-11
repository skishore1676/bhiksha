from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time

import pytest

from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    QuoteTapeMark,
    analyze_cases,
    build_historical_coverage_report,
    experiment_spec_hash,
    load_fixture_cases,
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
    experiment = {
        "fill_latency_ms": 0, "max_freshness_ms": 2000, "max_sequence_gap": 1,
        "evaluator_version": "profile-evaluator-v1", "fill_model_version": "next-fresh-natural-bid-v2",
        "quote_source": "public_cache", "quote_feed": "existing_position_quote_cache_v1",
    }
    bids = [2.10, 2.70, 2.75, 3.40, 3.35]
    return {
        "cohort_id": "C1", "trade_id": "T1", "cluster_id": "2026-07-09:QQQ",
        "deployment_id": "qqq-live",
        "symbol": "QQQ", "option_symbol": "OPT", "entry_timestamp": ENTRY.isoformat(),
        "entry_premium": 2.0, "quantity": 10, "profile": profile, "legacy": legacy,
        "experiment": experiment,
        "experiment_spec_hash": experiment_spec_hash(profile, legacy, experiment),
        "quotes": [{
            "sequence": i, "quote_at": (ENTRY+timedelta(seconds=i*15)).isoformat(),
            "source": "public_cache",
            "feed": "existing_position_quote_cache_v1",
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


def test_excursions_stop_at_later_virtual_exit(tmp_path: Path) -> None:
    raw=_raw_case()
    raw["quotes"].append({
        "sequence": 6, "source": "public_cache", "feed": "existing_position_quote_cache_v1",
        "quote_at": (ENTRY+timedelta(seconds=90)).isoformat(),
        "received_at": (ENTRY+timedelta(seconds=90,milliseconds=100)).isoformat(),
        "bid": 100.0, "ask": 100.05, "last": 100.02,
    })
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["mfe_pct"] == 70.0


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
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "experiment_spec_hash_mismatch"


def test_tape_end_before_second_arm_fill_is_right_censored(tmp_path: Path) -> None:
    raw=_raw_case(); raw["quotes"]=raw["quotes"][:4]
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["status"] == "insufficient_data"
    assert row["insufficient_reason"] == "right_censored:profile"


def test_pre_entry_quote_and_feed_transition_are_censored(tmp_path: Path) -> None:
    raw=_raw_case(); raw["quotes"][0]["quote_at"]=(ENTRY-timedelta(seconds=1)).isoformat()
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "quote_precedes_entry"
    raw=_raw_case(); raw["quotes"][2]["feed"]="other_feed"
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "quote_source_or_feed_transition"


def test_experiment_knobs_are_frozen_and_emitted(tmp_path: Path) -> None:
    raw=_raw_case(); report=analyze_cases(_load(tmp_path,raw))
    assert report["cases"][0]["experiment_spec"]["fill_latency_ms"] == 0
    assert report["experiment_spec_hashes"] == [raw["experiment_spec_hash"]]
    raw["experiment"]["fill_latency_ms"]=20_000
    row=analyze_cases(_load(tmp_path,raw))["cases"][0]
    assert row["insufficient_reason"] == "experiment_spec_hash_mismatch"


def test_unsupported_or_heterogeneous_experiment_versions_cannot_aggregate(tmp_path: Path) -> None:
    raw=_raw_case(); raw["experiment"]["evaluator_version"]="future"
    raw["experiment_spec_hash"]=experiment_spec_hash(raw["profile"],raw["legacy"],raw["experiment"])
    assert analyze_cases(_load(tmp_path,raw))["cases"][0]["insufficient_reason"] == "unsupported_evaluator_or_fill_model_version"

    first=_raw_case(); second=_raw_case(); second["cohort_id"]="C2"; second["trade_id"]="T2"; second["cluster_id"]="cluster-2"
    second["experiment"]["fill_latency_ms"]=1
    second["experiment_spec_hash"]=experiment_spec_hash(second["profile"],second["legacy"],second["experiment"])
    path=tmp_path/"mixed.json"; path.write_text(json.dumps({"cases":[first,second]}))
    summary=analyze_cases(load_fixture_cases(path))["summary"]
    assert summary["homogeneous_experiment_spec"] is False
    assert summary["confidence"]["indicator"] == "heterogeneous_experiment_specs"


def test_negative_aggregate_can_never_claim_directional_uplift(tmp_path: Path) -> None:
    raws=[]
    for idx, delta_shape in enumerate([1,1,1,1,1,1,1,-100]):
        raw=_raw_case(); raw["cohort_id"]=f"C{idx}"; raw["trade_id"]=f"T{idx}"
        raw["cluster_id"]=f"cluster-{idx}"
        # Produce seven small positive profile deltas and one dominating loss.
        if delta_shape < 0:
            raw["quantity"]=100
            raw["quotes"][4]["bid"]=0.1; raw["quotes"][4]["ask"]=0.15
        raws.append(raw)
    path=tmp_path/"many.json"; path.write_text(json.dumps({"cases":raws}))
    summary=analyze_cases(load_fixture_cases(path))["summary"]
    assert summary["total_paired_delta_pnl_usd"] < 0
    assert summary["confidence"]["indicator"] != "directional_profile_uplift"


def test_repository_is_idempotent_and_conflicts_do_not_overwrite(tmp_path: Path) -> None:
    repo=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); repo.initialize(); raw=_raw_case()
    repo.register_cohort(raw); repo.register_cohort(raw)
    q=QuoteTapeMark(1,"public_cache","existing_position_quote_cache_v1",ENTRY+timedelta(seconds=1),ENTRY+timedelta(seconds=1,milliseconds=10),2.0,2.1,2.05)
    repo.append_quote("C1",q)
    # A new process/repository instance can replay the same append after restart.
    restarted=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); restarted.append_quote("C1",q)
    with pytest.raises(ValueError):
        repo.append_quote("C1",QuoteTapeMark(1,"public_cache","existing_position_quote_cache_v1",q.quote_at,q.received_at,1.9,2.1,2.0))
    changed=dict(raw); changed["quantity"]=11
    with pytest.raises(ValueError): repo.register_cohort(changed)
    with sqlite3.connect(tmp_path/"lab.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM exit_edge_quote_tape").fetchone()[0] == 1


def test_repository_rejects_orphans_identity_drift_and_loads_persisted_censor(tmp_path: Path) -> None:
    repo=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); repo.initialize(); raw=_raw_case()
    q=QuoteTapeMark(1,"public_cache","existing_position_quote_cache_v1",ENTRY+timedelta(seconds=1),ENTRY+timedelta(seconds=1,milliseconds=10),2.0,2.1,2.05)
    with pytest.raises(ValueError): repo.append_quote("MISSING",q)
    repo.register_cohort(raw)
    for key,value in (("deployment_id","other"),("symbol","SPY"),("entry_timestamp",(ENTRY+timedelta(seconds=1)).isoformat())):
        changed=dict(raw); changed[key]=value
        with pytest.raises(ValueError): repo.register_cohort(changed)
    repo.append_quote("C1",q); repo.record_censor("C1","feed_gap")
    case=repo.load_case("C1")
    assert case.persisted_censor_reason == "feed_gap"
    assert analyze_cases([case])["cases"][0]["insufficient_reason"] == "persisted_censor:feed_gap"


def test_exclusive_lock_returns_quickly_without_delaying_caller(tmp_path: Path) -> None:
    repo=ProspectiveQuoteTapeRepository(tmp_path/"lab.db"); repo.initialize(); raw=_raw_case(); repo.register_cohort(raw)
    blocker=sqlite3.connect(tmp_path/"lab.db", timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    q=QuoteTapeMark(1,"public_cache","existing_position_quote_cache_v1",ENTRY+timedelta(seconds=1),ENTRY+timedelta(seconds=1,milliseconds=10),2.0,2.1,2.05)
    started=time.perf_counter()
    try:
        assert repo.try_append_quote("C1",q) is False
    finally:
        elapsed=time.perf_counter()-started; blocker.rollback(); blocker.close()
    assert elapsed < 0.05


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
