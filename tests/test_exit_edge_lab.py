from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time

import pytest

from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    QuoteTapeMark,
    LEGACY_RISK_ENVELOPE_EXPERIMENT_ID,
    LEGACY_RISK_ENVELOPE_EXPERIMENT_SCHEMA_VERSION,
    SHADOW_CANDIDATE_IDS,
    analyze_cases,
    analyze_prospective_repository,
    build_risk_envelope_experiment,
    build_historical_coverage_report,
    experiment_spec_hash,
    load_fixture_cases,
)
from bhiksha.execution.exit_policy import canonical_policy_hash

ENTRY = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)


def _control_policy() -> dict:
    return {
        "policy_id": "exit.premium_envelope.trend_continuation.control.v1",
        "stop_family": "premium_pct",
        "stop_anchor": "filled_option_premium",
        "exit_family": "profile_ladder",
        "target_model": "staged_r",
        "target_r": 2.0,
        "hard_flat_time_et": "15:55",
        "option_stop_fallback_pct": 0.45,
        "target_order_mode": "virtual_or_broker",
        "source_config_id": None,
        "parameters": {},
        "policy_schema_version": "exit-policy.v1",
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.6,
        "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.45,
        "no_progress_seconds": 2700,
        "max_hold_seconds": 10800,
        "high_water_giveback_policy": "MODERATE",
        "giveback_arm_r": 1.25,
        "giveback_retrace_fraction": 0.5,
        "risk_envelope_enabled": False,
        "risk_envelope_activation_r": None,
        "risk_envelope_initial_floor_r": None,
        "risk_envelope_curvature": None,
        "risk_envelope_floor_at_t1_r": None,
        "risk_envelope_ratchet_step_r": None,
        "breakeven_after_t1": True,
        "eod_flat": True,
    }


def _raw_case() -> dict:
    profile = {
        "profile_exit_id": "profile__trend_continuation", "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.45, "target_1_r": 1.0, "target_2_r": 2.0,
        "target_1_quantity": 0.6, "high_water_giveback_policy": "MODERATE",
        "giveback_arm_r": 1.25, "giveback_retrace_fraction": 0.5,
        "eod_flat": True, "hard_flat_time_et": "15:55",
    }
    legacy = {"stop_loss_pct": 0.35, "profit_target_pct": 0.35, "hard_flat_time_et": "15:55"}
    experiment = {
        "fill_latency_ms": 0, "max_freshness_ms": 2000, "max_sequence_gap": 1,
        "evaluator_version": "profile-evaluator-v1", "fill_model_version": "next-fresh-natural-bid-v2",
        "quote_source": "public_cache", "quote_feed": "existing_position_quote_cache_v1",
    }
    control = _control_policy()
    experiment["risk_envelope"] = build_risk_envelope_experiment(
        control,
        control_policy_hash=canonical_policy_hash(control),
    )
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


def test_repository_reports_w1_w2_w3_maturity_without_promotion_claim(
    tmp_path: Path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    repository.initialize()
    repository.register_cohort(_raw_case())

    maturity = repository.cohort_maturity_summary(
        as_of=ENTRY + timedelta(days=15)
    )

    assert maturity["checkpoints"]["W1"]["mature_cohort_count"] == 1
    assert maturity["checkpoints"]["W2"]["mature_cohort_count"] == 1
    assert maturity["checkpoints"]["W3"]["mature_cohort_count"] == 0


def test_legacy_three_arm_experiment_remains_replayable(tmp_path: Path) -> None:
    raw = _raw_case()
    current = raw["experiment"]["risk_envelope"]
    raw["experiment"]["risk_envelope"] = {
        "schema_version": LEGACY_RISK_ENVELOPE_EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": LEGACY_RISK_ENVELOPE_EXPERIMENT_ID,
        "arms": [
            {
                key: value
                for key, value in arm.items()
                if key != "candidate_type"
            }
            for arm in current["arms"]
            if arm["candidate_id"] in {"control", "variant_a", "variant_b"}
        ],
    }
    raw["experiment_spec_hash"] = experiment_spec_hash(
        raw["profile"], raw["legacy"], raw["experiment"]
    )

    row = analyze_cases(_load(tmp_path, raw))["cases"][0]

    assert set(row["risk_envelope_outcomes"]) == {
        "control",
        "variant_a",
        "variant_b",
    }


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
    assert set(row["risk_envelope_outcomes"]) == set(SHADOW_CANDIDATE_IDS)
    assert len(row["risk_envelope_observations"]) == (
        len(_raw_case()["quotes"]) * len(SHADOW_CANDIDATE_IDS)
    )
    assert row["missingness"]["identity_or_timestamp_rows"] == 0


def test_control_a_b_have_distinct_identity_and_isolated_monotonic_state(
    tmp_path: Path,
) -> None:
    raw = _raw_case()
    # Rise to 0.8R, retrace through both distinct locked floors, then provide
    # the next fresh bid used by the existing modeled fill convention.
    raw["quotes"] = []
    for sequence, bid in enumerate([2.10, 2.56, 2.40, 2.20], start=1):
        at = ENTRY + timedelta(seconds=sequence * 15)
        raw["quotes"].append(
            {
                "sequence": sequence,
                "source": "public_cache",
                "feed": "existing_position_quote_cache_v1",
                "quote_at": at.isoformat(),
                "received_at": (at + timedelta(milliseconds=100)).isoformat(),
                "bid": bid,
                "ask": bid + 0.05,
                "last": bid + 0.02,
            }
        )
    row = analyze_cases(_load(tmp_path, raw))["cases"][0]
    states = {
        state["candidate_id"]: state
        for state in row["shadow_envelope_states"]
    }
    assert set(states) == set(SHADOW_CANDIDATE_IDS) - {"control"}
    assert states["variant_a"]["candidate_policy_hash"] != states["variant_b"][
        "candidate_policy_hash"
    ]
    frozen_arms = {
        arm["candidate_id"]: arm
        for arm in raw["experiment"]["risk_envelope"]["arms"]
    }
    assert states["variant_a"]["candidate_policy_hash"] == frozen_arms[
        "variant_a"
    ]["candidate_policy_hash"]
    assert states["variant_b"]["candidate_policy_hash"] == frozen_arms[
        "variant_b"
    ]["candidate_policy_hash"]
    assert frozen_arms["variant_a"]["candidate_overlay_hash"] == (
        "15351843271c171bf400a570a77b98fdb9b676263b8888acc6d521c1a2d26e8e"
    )
    assert frozen_arms["variant_b"]["candidate_overlay_hash"] == (
        "c681ce54724540a48985e29973643a267dbfc073d4bd0a5d8c6bd0746efe5a2d"
    )
    assert states["variant_a"]["locked_floor_r"] > states["variant_b"][
        "locked_floor_r"
    ]
    observations = row["risk_envelope_observations"]
    for candidate_id in ("variant_a", "variant_b"):
        floors = [
            item["locked_floor_r"]
            for item in observations
            if item["candidate_id"] == candidate_id
            and item["candidate_floor_r"] is not None
        ]
        assert floors == sorted(floors)
        assert all(
            item["control_decision"]
            for item in observations
            if item["candidate_id"] == candidate_id
        )


def test_shadow_envelope_ratchets_only_after_configured_step(tmp_path: Path) -> None:
    raw = _raw_case()
    # This favorable move just crosses the fixed 0.25R activation but improves
    # the curve by less than the catalog's fixed 0.10R ratchet step.
    raw["quotes"][0]["bid"] = 2.18
    raw["quotes"][0]["ask"] = 2.23
    raw["quotes"][0]["last"] = 2.20
    raw["quotes"][1]["bid"] = 2.18
    raw["quotes"][1]["ask"] = 2.23
    raw["quotes"][1]["last"] = 2.20

    row = analyze_cases(_load(tmp_path, raw))["cases"][0]
    observations = [
        item
        for item in row["risk_envelope_observations"]
        if item["candidate_id"] == "variant_a"
    ]

    assert observations[0]["candidate_floor_r"] > -1.0
    assert observations[0]["locked_floor_r"] == -1.0
    assert observations[0]["would_ratchet"] is False


def test_shadow_candidate_state_repository_rejects_identity_and_floor_regression(
    tmp_path: Path,
) -> None:
    raw = _raw_case()
    case = _load(tmp_path, raw)[0]
    row = analyze_cases([case])["cases"][0]
    repo = ProspectiveQuoteTapeRepository(tmp_path / "lab.db")
    repo.initialize()
    from bhiksha.ops.exit_edge_lab import ShadowEnvelopeState

    states = tuple(
        ShadowEnvelopeState(**state)
        for state in row["shadow_envelope_states"]
    )
    repo.persist_shadow_envelope_states(states)
    loaded = repo.load_shadow_envelope_states("T1")
    assert {state.candidate_id for state in loaded} == (
        set(SHADOW_CANDIDATE_IDS) - {"control"}
    )
    regressed = replace(
        states[0],
        locked_floor_r=float(states[0].locked_floor_r) - 0.1,
    )
    with pytest.raises(ValueError, match="floor regressed"):
        repo.persist_shadow_envelope_states((regressed,))
    cleared = replace(states[0], locked_floor_r=None)
    with pytest.raises(ValueError, match="cannot be cleared"):
        repo.persist_shadow_envelope_states((cleared,))
    equal_revision_drift = replace(
        states[0],
        last_observation_id="different-observation",
    )
    with pytest.raises(ValueError, match="equal revision is not idempotent"):
        repo.persist_shadow_envelope_states((equal_revision_drift,))


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
    assert row["insufficient_reason"] == (
        "right_censored:" + ",".join(SHADOW_CANDIDATE_IDS)
    )


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


def test_unhashed_fixture_cannot_auto_sign_itself(tmp_path: Path) -> None:
    raw=_raw_case(); raw.pop("experiment_spec_hash")
    with pytest.raises(ValueError, match="explicit experiment_spec_hash"):
        _load(tmp_path,raw)


def test_pre_increment_fixture_exposes_missing_envelope_identity() -> None:
    fixture = Path(__file__).parent / "fixtures/exit_edge_lab/paired_quote_tape.json"
    row = analyze_cases(load_fixture_cases(fixture))["cases"][0]
    assert row["insufficient_reason"] == (
        "missing_risk_envelope_experiment_identity"
    )


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


def test_live_repository_missing_registration_blocks_subset_inference(tmp_path: Path) -> None:
    repo = ProspectiveQuoteTapeRepository(tmp_path / "lab.db")
    repo.initialize()
    raw = _raw_case()
    repo.register_cohort(raw)
    case = _load(tmp_path, raw)[0]
    for quote in case.quotes:
        repo.append_quote(case.cohort_id, quote)
    assert repo.try_record_registration_attempt({
        "trade_id": "T1", "deployment_id": "qqq-live", "symbol": "QQQ",
        "option_symbol": "OPT", "observed_at": ENTRY.isoformat(), "eligible": True,
        "cohort_id": "C1", "outcome": "registered", "reason": None,
    })
    assert repo.try_record_registration_attempt({
        "trade_id": "T2", "deployment_id": "qqq-live", "symbol": "QQQ",
        "option_symbol": "OPT2", "observed_at": ENTRY.isoformat(), "eligible": True,
        "cohort_id": "C2", "outcome": "registration_queue_full",
        "reason": "cohort_registration_queue_full",
    })
    report = analyze_prospective_repository(repo)
    summary = report["summary"]
    assert summary["registration_denominator"]["eligible_attempts"] == 2
    assert summary["registration_denominator"]["registered_cohorts"] == 1
    assert summary["inference_eligible"] is False
    assert summary["confidence"]["indicator"] == "live_collection_inference_blocked"
    assert "eligible_registration_denominator_incomplete" in summary["inference_blockers"]


@pytest.mark.parametrize(
    "health_mutation,blocker",
    [
        ({"schema_version": 2}, "live_health_schema_invalid"),
        ({"enabled": False}, "live_health_disabled"),
        ({"mode": "execution"}, "live_health_mode_invalid"),
        ({"enforcement_authority": True}, "live_health_enforcement_authority_invalid"),
        ({"promotion_eligible": True}, "live_health_promotion_authority_invalid"),
        ({"broker_calls_added": 1}, "live_health_broker_calls_added"),
        ({"registration_failures": 1}, "live_health_registration_failure"),
    ],
)
def test_live_repository_health_contract_blocks_inference(
    tmp_path: Path,
    health_mutation: dict,
    blocker: str,
) -> None:
    repo = ProspectiveQuoteTapeRepository(tmp_path / "lab.db")
    repo.initialize()
    raw = _raw_case()
    repo.register_cohort(raw)
    case = _load(tmp_path, raw)[0]
    for quote in case.quotes:
        repo.append_quote(case.cohort_id, quote)
    repo.try_record_registration_attempt({
        "trade_id": "T1", "deployment_id": "qqq-live", "symbol": "QQQ",
        "option_symbol": "OPT", "observed_at": ENTRY.isoformat(), "eligible": True,
        "cohort_id": "C1", "outcome": "registered", "reason": None,
    })
    health = {
        "schema_version": 1,
        "enabled": True,
        "mode": "observational_shadow_only",
        "enforcement_authority": False,
        "promotion_eligible": False,
        "broker_calls_added": 0,
        "storage_failures": 0,
        "dropped_observations": 0,
        "missing_registration_attempts": 0,
        "registration_failures": 0,
    }
    health.update(health_mutation)
    (tmp_path / "exit_edge_live_status.json").write_text(
        json.dumps(health),
        encoding="utf-8",
    )

    summary = analyze_prospective_repository(repo)["summary"]

    assert summary["inference_eligible"] is False
    assert blocker in summary["inference_blockers"]
    assert summary["confidence"]["indicator"] == "live_collection_inference_blocked"


def test_unexpected_risk_envelope_experiment_id_is_rejected(tmp_path: Path) -> None:
    raw = _raw_case()
    raw["experiment"]["risk_envelope"]["experiment_id"] = "self-signed-replacement"

    with pytest.raises(ValueError, match="unsupported risk-envelope experiment_id"):
        raw["experiment_spec_hash"] = experiment_spec_hash(
            raw["profile"],
            raw["legacy"],
            raw["experiment"],
        )


def test_self_resigned_risk_envelope_catalog_is_rejected(tmp_path: Path) -> None:
    raw = _raw_case()
    experiment = raw["experiment"]["risk_envelope"]
    variant_a = next(
        arm for arm in experiment["arms"] if arm["candidate_id"] == "variant_a"
    )
    variant_a["canonical_policy"]["risk_envelope_curvature"] = 9.0
    variant_a["candidate_policy_hash"] = canonical_policy_hash(
        variant_a["canonical_policy"]
    )

    with pytest.raises(
        ValueError,
        match="does not match the fixed shadow registry",
    ):
        raw["experiment_spec_hash"] = experiment_spec_hash(
            raw["profile"],
            raw["legacy"],
            raw["experiment"],
        )


def test_historical_cutoff_excludes_future_quotes_and_censor(tmp_path: Path) -> None:
    repo = ProspectiveQuoteTapeRepository(tmp_path / "lab.db")
    repo.initialize()
    raw = _raw_case()
    repo.register_cohort(raw)
    case = _load(tmp_path, raw)[0]
    cutoff = ENTRY + timedelta(seconds=20)
    for quote in case.quotes:
        repo.append_quote(
            case.cohort_id,
            replace(
                quote,
                quote_at=quote.quote_at + timedelta(days=1),
                received_at=quote.received_at + timedelta(days=1),
            ),
        )
    repo.record_censor(case.cohort_id, "session_shutdown")
    assert repo.try_record_registration_attempt({
        "trade_id": "T1", "deployment_id": "qqq-live", "symbol": "QQQ",
        "option_symbol": "OPT", "observed_at": ENTRY.isoformat(), "eligible": True,
        "cohort_id": "C1", "outcome": "registered", "reason": None,
    })
    health = {
        "schema_version": 1,
        "enabled": True,
        "mode": "observational_shadow_only",
        "enforcement_authority": False,
        "promotion_eligible": False,
        "broker_calls_added": 0,
        "storage_failures": 0,
        "dropped_observations": 0,
        "missing_registration_attempts": 0,
        "registration_failures": 0,
    }
    (tmp_path / "exit_edge_live_status.json").write_text(
        json.dumps(health),
        encoding="utf-8",
    )

    report = analyze_prospective_repository(
        repo,
        observed_at_end=cutoff,
    )

    assert report["summary"]["paired_count"] == 0
    assert report["summary"]["persisted_censor_count"] == 0
    assert report["cases"][0]["status"] == "insufficient_data"
    assert all(
        not row.get("risk_envelope_observations")
        for row in report["cases"]
    )


def test_live_readback_repository_is_strictly_read_only_and_never_creates_typo(tmp_path: Path) -> None:
    missing = tmp_path / "typo.db"
    repo = ProspectiveQuoteTapeRepository(missing, read_only=True)
    with pytest.raises((sqlite3.OperationalError, ValueError)):
        repo.list_cohort_ids()
    assert not missing.exists()


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
