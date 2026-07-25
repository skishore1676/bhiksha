from copy import deepcopy
from datetime import date
import json

from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository
from bhiksha.ops.exit_edge_weekly import (
    build_exit_edge_weekly_evidence,
    stable_digest,
)


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
    assert evidence["weekly"]["paired_count"] is None
    assert evidence["cumulative"]["paired_count"] is None
    assert evidence["inference"]["decision_ready"] is False
    assert evidence["receipt"]["status"] == "ok"


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
    assert evidence["verdict"]["status"] == "collecting_inference_blocked"
    assert "no_registered_cohorts" in evidence["inference"]["blockers"]
    assert evidence["cumulative"]["paired_count"] == 0
    assert evidence["cumulative"]["risk_envelope_candidate_vs_control"] == {
        "variant_a": {
            "paired_count": 0,
            "total_delta_pnl_usd": None,
            "mean_delta_pnl_usd": None,
            "envelope_exit_count": 0,
        },
        "variant_b": {
            "paired_count": 0,
            "total_delta_pnl_usd": None,
            "mean_delta_pnl_usd": None,
            "envelope_exit_count": 0,
        },
    }


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
