from copy import deepcopy
from datetime import UTC, date, datetime
import importlib.util
import json
import os
from pathlib import Path

from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    QuoteTapeMark,
)
from bhiksha.ops.exit_edge_weekly import (
    _cohort_rows,
    _maturity_stage,
    _v2_summary,
    build_exit_edge_weekly_evidence,
    stable_digest,
)
from tests.test_exit_edge_lab import _raw_case


WEEK_START = date(2026, 7, 20)
WEEK_END = date(2026, 7, 24)


def _health(*, updated_at: str = "2026-07-24T20:10:00+00:00") -> dict:
    return {
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
        "worker_alive": False,
        "updated_at": updated_at,
    }


def _authorized_canary() -> dict:
    return {
        "deployment_id": "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
        "symbol": "IWM",
        "candidate_id": "safety_stack",
        "candidate_overlay_hash": "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e",
        "runtime_source_policy_hash": "runtime-hash",
        "authorization_id": "test-auth",
        "start_at": "2026-07-20T00:00:00+00:00",
        "expires_at": "2026-08-01T00:00:00+00:00",
        "authorized_deployment_id": "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
        "authorized_symbol": "IWM",
        "authorized_active_plan_id": "active-plan-test",
        "startup_authorization_fingerprint": "f" * 64,
        "rollback_action": "disable_canary_restore_control",
        "max_premium_cap_fraction": 0.20,
        "base_max_trade_premium_usd": 2_000.0,
        "effective_max_trade_premium_usd": 400.0,
        "runtime_mode": "live_approval_gated",
        "dte_min": 4,
        "dte_max": 7,
        "dte_fallback_policy": "strict",
        "max_contracts": 1,
    }


def _build(tmp_path, *, configured: bool, health: dict | None = None):
    db = tmp_path / "exit_edge.sqlite3"
    status = tmp_path / "exit_edge_live_status.json"
    if health is not None:
        status.write_text(json.dumps(health), encoding="utf-8")
    return build_exit_edge_weekly_evidence(
        db_path=db,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=configured,
        live_envelope_enabled_count=0,
    )


def test_missing_collection_is_explicitly_unavailable_not_zero_edge(tmp_path) -> None:
    evidence = _build(tmp_path, configured=False)

    assert evidence["verdict"]["status"] == "not_collecting"
    assert evidence["weekly"]["paired_count"] == 0
    assert evidence["cumulative"]["paired_count"] == 0
    assert evidence["inference"]["decision_ready"] is False
    assert evidence["receipt"]["status"] == "ok"


def test_weekly_cohort_rows_preserve_full_policy_and_execution_identity() -> None:
    rows = _cohort_rows(
        [
            {
                "status": "paired",
                "symbol": "IWM",
                "cluster_id": "session-et-underlying-v1:2026-07-24:IWM",
                "cohort_dimensions": {
                    "strategy_policy_hash": "runtime-policy-hash",
                    "configured_dte_min": 4,
                    "configured_dte_max": 7,
                    "dte_fallback_policy": "strict",
                    "runtime_mode": "live_approval_gated",
                    "authorization_mode": "live",
                    "authorization_id": "auth",
                    "selected_dte": 5,
                    "selected_abs_delta": 0.31,
                    "entry_spread_pct": 0.08,
                },
                "experiment_spec": {
                    "risk_envelope": {
                        "arms": [
                            {
                                "candidate_id": "control",
                                "candidate_policy_id": "control-policy",
                                "candidate_policy_hash": "producer-hash",
                            }
                        ]
                    }
                },
            }
        ]
    )

    assert rows == [
        {
            "producer": "bhiksha",
            "strategy_policy_id": "control-policy",
            "strategy_policy_hash": "runtime-policy-hash",
            "configured_dte_min": 4,
            "configured_dte_max": 7,
            "dte_fallback_policy": "strict",
            "runtime_mode": "live_approval_gated",
            "authorization_mode": "live",
            "authorization_id": "auth",
            "symbol": "IWM",
            "cluster_id": "session-et-underlying-v1:2026-07-24:IWM",
            "dte_bucket": "4-7",
            "delta_bucket": "0.25-0.34",
            "liquidity_bucket": ">5-10%",
            "paired_count": 1,
            "case_count": 1,
            "unknown_reasons": {},
        }
    ]


def test_weekly_cohort_rows_preserve_zero_dte_as_integer_and_bucket() -> None:
    rows = _cohort_rows(
        [
            {
                "status": "insufficient_data",
                "symbol": "IWM",
                "cluster_id": "cluster",
                "cohort_dimensions": {
                    "configured_dte_min": 0,
                    "configured_dte_max": 3,
                    "selected_dte": 0,
                    "authorization_mode": "shadow",
                },
                "experiment_spec": {"risk_envelope": {"arms": []}},
            }
        ]
    )

    assert rows[0]["configured_dte_min"] == 0
    assert rows[0]["configured_dte_max"] == 3
    assert rows[0]["dte_bucket"] == "0-3"
    assert "configured_dte_min" not in rows[0]["unknown_reasons"]


def test_w3_age_without_complete_terminal_pairs_is_not_economics() -> None:
    maturity = {
        "checkpoints": {
            "W1": {"mature_cohort_count": 1},
            "W2": {"mature_cohort_count": 1},
            "W3": {"mature_cohort_count": 1},
        }
    }
    cumulative = {
        "terminal_paired_count": 0,
        "post_exit_complete_trade_count": 0,
        "right_censored_trade_count": 1,
        "candidates": {
            candidate: {"paired_count": 0}
            for candidate in (
                "control",
                "variant_a",
                "variant_b",
                "common_giveback",
                "safety_stack",
                "profit_preservation",
            )
        },
    }

    assert _maturity_stage(maturity, cumulative=cumulative) == "insufficient"


def test_configured_collector_with_stale_health_is_not_current_evidence(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    evidence = _build(
        tmp_path,
        configured=True,
        health=_health(updated_at="2026-07-10T20:10:00+00:00"),
    )

    assert evidence["collection"]["freshness"]["status"] == "stale"
    assert evidence["verdict"]["status"] == "stale_collection"
    assert evidence["inference"]["decision_ready"] is False


def test_current_empty_collector_reports_blocked_inference_not_zero_uplift(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    evidence = _build(tmp_path, configured=True, health=_health())

    assert evidence["collection"]["freshness"]["status"] == "fresh"
    assert evidence["verdict"]["status"] == "insufficient"
    assert "no_registered_cohorts" in evidence["inference"]["blockers"]
    assert evidence["cumulative"]["paired_count"] == 0
    candidates = evidence["cumulative"][
        "risk_envelope_candidate_vs_control"
    ]
    assert set(candidates) == {
        "variant_a",
        "variant_b",
        "common_giveback",
        "safety_stack",
        "profit_preservation",
    }
    assert all(candidate["paired_count"] == 0 for candidate in candidates.values())
    assert evidence["maturity_windows"]["checkpoints"]["W1"][
        "evidence_status"
    ] == "insufficient_evidence"


def test_receipt_is_stable_across_generation_time_and_sensitive_to_content(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    first = _build(tmp_path, configured=True, health=_health())
    second = _build(tmp_path, configured=True, health=_health())

    assert first["receipt"]["sha256"] == second["receipt"]["sha256"]
    changed = deepcopy(first)
    changed["verdict"]["reason"] = "tampered"
    assert stable_digest(changed) != first["receipt"]["sha256"]


def test_live_envelope_or_broker_reachability_forces_safety_block(tmp_path) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    status = tmp_path / "exit_edge_live_status.json"
    status.write_text(json.dumps({**_health(), "broker_calls_added": 1}), encoding="utf-8")
    evidence = build_exit_edge_weekly_evidence(
        db_path=repository.path,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=True,
        live_envelope_enabled_count=1,
    )

    assert evidence["verdict"]["status"] == "safety_blocked"
    assert evidence["inference"]["decision_ready"] is False


def test_one_authorized_safety_stack_canary_is_reported_without_safety_block(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    status = tmp_path / "exit_edge_live_status.json"
    status.write_text(json.dumps(_health()), encoding="utf-8")

    evidence = build_exit_edge_weekly_evidence(
        db_path=repository.path,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=True,
        live_envelope_enabled_count=1,
        authorized_canaries=[
            {
                "deployment_id": (
                    "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
                ),
                "symbol": "IWM",
                "candidate_id": "safety_stack",
                "candidate_overlay_hash": (
                    "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e"
                ),
                "runtime_source_policy_hash": "runtime-hash",
                "authorization_id": "test-auth",
                "start_at": "2026-07-20T00:00:00+00:00",
                "expires_at": "2026-08-01T00:00:00+00:00",
                "authorized_deployment_id": (
                    "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
                ),
                "authorized_symbol": "IWM",
                "authorized_active_plan_id": "active-plan-test",
                "startup_authorization_fingerprint": "f" * 64,
                "rollback_action": "disable_canary_restore_control",
                "max_premium_cap_fraction": 0.20,
                "base_max_trade_premium_usd": 2_000.0,
                "effective_max_trade_premium_usd": 400.0,
                "runtime_mode": "live_approval_gated",
                "dte_min": 4,
                "dte_max": 7,
                "dte_fallback_policy": "strict",
                "max_contracts": 1,
            }
        ],
    )

    assert evidence["safety"]["canary_contract_valid"] is True
    assert evidence["verdict"]["status"] == "insufficient"


def test_expired_canary_is_disarmed_and_safety_blocked(tmp_path) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    status = tmp_path / "exit_edge_live_status.json"
    status.write_text(json.dumps(_health()), encoding="utf-8")
    evidence = build_exit_edge_weekly_evidence(
        db_path=repository.path,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=True,
        live_envelope_enabled_count=1,
        authorized_canaries=[
            {
                "deployment_id": "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
                "symbol": "IWM",
                "candidate_id": "safety_stack",
                "candidate_overlay_hash": "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e",
                "runtime_source_policy_hash": "runtime-hash",
                "authorization_id": "expired-auth",
                "start_at": "2026-07-01T00:00:00+00:00",
                "expires_at": "2026-07-19T00:00:00+00:00",
                "authorized_deployment_id": "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
                "authorized_symbol": "IWM",
                "authorized_active_plan_id": "active-plan-test",
                "startup_authorization_fingerprint": "f" * 64,
                "rollback_action": "disable_canary_restore_control",
                "max_premium_cap_fraction": 0.20,
                "base_max_trade_premium_usd": 2_000.0,
                "effective_max_trade_premium_usd": 400.0,
                "runtime_mode": "live_approval_gated",
                "dte_min": 4,
                "dte_max": 7,
                "dte_fallback_policy": "strict",
                "max_contracts": 1,
            }
        ],
    )

    assert evidence["deployment"]["state"] == "safety_blocked"
    assert evidence["arming"]["armed_deployment_count"] == 0
    assert evidence["arming"]["authorized_canary"] is None
    assert "authorization_expired" in evidence["safety"]["canary_contract_errors"]
    assert evidence["verdict"]["status"] == "safety_blocked"


def test_rollback_latch_overrides_valid_canary_arming_and_validates(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    status = tmp_path / "exit_edge_live_status.json"
    status.write_text(json.dumps(_health()), encoding="utf-8")
    latch = {
        "deployment_id": _authorized_canary()["deployment_id"],
        "reason": "stop_handoff_unproved:ambiguous",
        "latched_at": "2026-07-24T19:00:00+00:00",
    }

    evidence = build_exit_edge_weekly_evidence(
        db_path=repository.path,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=True,
        live_envelope_enabled_count=1,
        authorized_canaries=[_authorized_canary()],
        rollback_latches=[latch],
    )

    assert evidence["deployment"]["state"] == "disarmed_rollback_latched"
    assert evidence["arming"] == {
        "armed_deployment_count": 0,
        "armed_candidate_id": None,
        "authorized_canary": None,
        "rollback_latch": latch,
    }
    assert evidence["safety"]["rollback_latched_count"] == 1
    assert evidence["verdict"]["status"] == "safety_blocked"
    _current_tradelab_validator()(
        evidence,
        expected_producer="bhiksha",
    )


def _current_tradelab_validator():
    repo = Path(__file__).resolve().parents[1]
    roots = [
        Path(os.environ["TRADELAB_REPO"])
        if os.environ.get("TRADELAB_REPO")
        else None,
        repo.parent / "tradelab",
        Path.home() / "code" / "tradelab",
    ]
    paths = [
        root / "scripts" / "review" / "trading_governance_review.py"
        for root in roots
        if root is not None
    ]
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        raise AssertionError(
            "current sister-repo TradeLab validator not found; "
            "set TRADELAB_REPO"
        )
    spec = importlib.util.spec_from_file_location(
        "current_tradelab_governance_review",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_exit_policy_v2


def test_empty_and_nonempty_packets_validate_with_current_tradelab(
    tmp_path,
) -> None:
    empty = _build(tmp_path, configured=False)
    validator = _current_tradelab_validator()
    validator(empty, expected_producer="bhiksha")

    experiment = empty["catalog"]
    arms = [
        {
            "candidate_id": item["candidate_id"],
            "candidate_policy_id": f"{item['candidate_id']}.policy",
            "candidate_policy_hash": item["policy_hash"],
        }
        for item in experiment["candidates"]
    ]
    case = {
        "status": "paired",
        "symbol": "IWM",
        "cluster_id": "cluster",
        "cohort_dimensions": {
            "strategy_policy_hash": arms[0]["candidate_policy_hash"],
            "configured_dte_min": 4,
            "configured_dte_max": 7,
            "dte_fallback_policy": "strict",
            "runtime_mode": "live_approval_gated",
            "authorization_mode": "live",
            "selected_dte": 5,
            "selected_abs_delta": 0.31,
            "entry_spread_pct": 0.08,
        },
        "experiment_spec": {"risk_envelope": {"arms": arms}},
        "missingness": {"arms_without_post_exit_quote": []},
    }
    summary = {
        "case_count": 1,
        "paired_count": 1,
        "homogeneous_experiment_spec": True,
        "risk_envelope_candidate_vs_control": {
            item["candidate_id"]: {
                "paired_count": 1,
                "total_delta_pnl_usd": 0.0,
                "mean_delta_pnl_usd": 0.0,
                "candidate_specific_exit_count": 0,
            }
            for item in experiment["candidates"]
            if item["candidate_id"] != "control"
        },
    }
    nonempty = deepcopy(empty)
    nonempty["weekly"] = _v2_summary(summary, [case])
    nonempty["cumulative"] = _v2_summary(summary, [case])
    nonempty["receipt"]["sha256"] = stable_digest(nonempty)
    validator(nonempty, expected_producer="bhiksha")


def test_real_weekly_packet_preserves_case_symbol_for_tradelab(
    tmp_path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()
    raw = _raw_case()
    repository.register_cohort(raw)
    for quote in raw["quotes"]:
        repository.append_quote(
            raw["cohort_id"],
            QuoteTapeMark(
                sequence=int(quote["sequence"]),
                source=str(quote["source"]),
                feed=str(quote["feed"]),
                quote_at=datetime.fromisoformat(quote["quote_at"]),
                received_at=datetime.fromisoformat(quote["received_at"]),
                bid=quote.get("bid"),
                ask=quote.get("ask"),
                last=quote.get("last"),
            ),
        )
    status = tmp_path / "exit_edge_live_status.json"
    status.write_text(json.dumps(_health()), encoding="utf-8")

    evidence = build_exit_edge_weekly_evidence(
        db_path=repository.path,
        status_path=status,
        week_start=WEEK_START,
        week_end=WEEK_END,
        collector_configured=True,
        live_envelope_enabled_count=0,
    )

    cohort = evidence["cumulative"]["cohorts"][0]
    assert cohort["symbol"] == "QQQ"
    assert "symbol" not in cohort["unknown_reasons"]
    _current_tradelab_validator()(
        evidence,
        expected_producer="bhiksha",
    )


def test_missing_safety_or_disabled_health_is_not_reportable(tmp_path) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "exit_edge.sqlite3")
    repository.initialize()

    missing_safety = _health()
    missing_safety.pop("broker_calls_added")
    evidence = _build(tmp_path, configured=True, health=missing_safety)
    assert evidence["verdict"]["status"] == "safety_blocked"

    disabled = _health()
    disabled["enabled"] = False
    evidence = _build(tmp_path, configured=True, health=disabled)
    assert evidence["verdict"]["status"] == "collection_unreadable"

    registration_failure = _health()
    registration_failure["registration_failures"] = 1
    evidence = _build(tmp_path, configured=True, health=registration_failure)
    assert evidence["verdict"]["status"] == "collection_unreadable"

    malformed = _health()
    malformed["broker_calls_added"] = "corrupt"
    evidence = _build(tmp_path, configured=True, health=malformed)
    assert evidence["verdict"]["status"] == "safety_blocked"
    assert evidence["receipt"]["status"] == "ok"
