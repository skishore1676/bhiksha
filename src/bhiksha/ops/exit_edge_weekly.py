"""Receipted weekly evidence for the broker-inert Dynamic Risk Envelope.

Bhiksha owns the observation facts.  This module reads the isolated Exit Edge
store, applies its existing inference guards, and emits a compact artifact for
TradeLab.  It never imports an order manager, creates quotes, or changes
trading configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    RISK_ENVELOPE_EXPERIMENT_ID,
    SHADOW_CANDIDATE_IDS,
    analyze_prospective_repository,
)
from bhiksha.ops.code_version import code_version_snapshot
from bhiksha.shared_kernel import ensure_kernel_on_path


ensure_kernel_on_path()
from mala_bhiksha_kernel import (  # noqa: E402
    load_protective_floor_conformance_vectors,
)


SCHEMA = "trading.exit_policy_weekly_evidence.v2"
REPORT_TYPE = "prospective_exit_policy_weekly_readback"
ET = ZoneInfo("America/New_York")
MAX_HEALTH_AGE = timedelta(hours=12)


@dataclass(slots=True, frozen=True)
class ExitEdgeWeeklyEvidenceWriteResult:
    evidence: dict[str, Any]
    json_path: Path


def write_exit_edge_weekly_evidence(
    *,
    db_path: str | Path,
    status_path: str | Path,
    output_dir: str | Path,
    week_start: date,
    week_end: date,
    collector_configured: bool,
    live_envelope_enabled_count: int,
    authorized_canaries: list[dict[str, Any]] | None = None,
    rollback_latches: list[dict[str, Any]] | None = None,
) -> ExitEdgeWeeklyEvidenceWriteResult:
    evidence = build_exit_edge_weekly_evidence(
        db_path=db_path,
        status_path=status_path,
        week_start=week_start,
        week_end=week_end,
        collector_configured=collector_configured,
        live_envelope_enabled_count=live_envelope_enabled_count,
        authorized_canaries=authorized_canaries,
        rollback_latches=rollback_latches,
    )
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"exit_policy_weekly_evidence_{week_end.isoformat()}.json"
    _atomic_json(path, evidence)
    return ExitEdgeWeeklyEvidenceWriteResult(evidence=evidence, json_path=path)


def build_exit_edge_weekly_evidence(
    *,
    db_path: str | Path,
    status_path: str | Path,
    week_start: date,
    week_end: date,
    collector_configured: bool,
    live_envelope_enabled_count: int,
    authorized_canaries: list[dict[str, Any]] | None = None,
    rollback_latches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if week_start > week_end:
        raise ValueError("week_start must be on or before week_end")

    db = Path(db_path)
    status = Path(status_path)
    health, health_error = _read_health(status)
    source_error: str | None = None
    weekly_summary = _empty_summary()
    cumulative_summary = _empty_summary()
    weekly_cases: list[dict[str, Any]] = []
    cumulative_cases: list[dict[str, Any]] = []
    experiment_spec_hashes: list[str] = []
    maturity = _empty_maturity(as_of=None)
    start_at = datetime.combine(week_start, time.min, tzinfo=ET).astimezone(UTC)
    end_at = datetime.combine(week_end, time.max, tzinfo=ET).astimezone(UTC)
    as_of = min(datetime.now(UTC), end_at)

    if db.is_file():
        try:
            repository = ProspectiveQuoteTapeRepository(db, read_only=True)
            weekly_report = analyze_prospective_repository(
                repository,
                health_path=status,
                observed_at_start=start_at,
                observed_at_end=end_at,
            )
            cumulative_report = analyze_prospective_repository(
                repository,
                health_path=status,
                observed_at_end=end_at,
            )
            weekly_summary = _report_summary(weekly_report)
            cumulative_summary = _report_summary(cumulative_report)
            weekly_cases = list(weekly_report.get("cases") or [])
            cumulative_cases = list(cumulative_report.get("cases") or [])
            maturity = _maturity_summary(
                repository.cohort_maturity_summary(as_of=as_of)
            )
            experiment_spec_hashes = list(
                cumulative_report.get("experiment_spec_hashes") or []
            )
        except (OSError, ValueError, TypeError) as exc:
            source_error = f"{type(exc).__name__}:{exc}"
        except Exception as exc:  # sqlite errors remain explicit evidence failures.
            source_error = f"{type(exc).__name__}:{exc}"
    else:
        source_error = "exit_edge_database_missing"

    canaries = [dict(item) for item in (authorized_canaries or [])]
    canary_deployment_ids = {
        str(item.get("deployment_id"))
        for item in canaries
        if str(item.get("deployment_id") or "").strip()
    }
    matching_latches = [
        dict(item)
        for item in (rollback_latches or [])
        if str(item.get("deployment_id") or "") in canary_deployment_ids
    ]
    rollback_latch = matching_latches[0] if matching_latches else None
    canary_errors = _canary_errors(
        enabled_count=int(live_envelope_enabled_count),
        canaries=canaries,
        as_of=as_of,
    )
    safety = {
        "advisory_only": True,
        "automatic_promotion": False,
        "decision_required": False,
        "collector_enforcement_authority": (
            health.get("enforcement_authority") if health is not None else None
        ),
        "broker_calls_added": (
            health.get("broker_calls_added") if health is not None else None
        ),
        "live_envelope_enabled_count": int(live_envelope_enabled_count),
        "authorized_canaries": canaries,
        "canary_contract_valid": not canary_errors,
        "canary_contract_errors": canary_errors,
    }
    collection = {
        "configured": bool(collector_configured),
        "mode": health.get("mode") if health is not None else None,
        "status_updated_at": health.get("updated_at") if health is not None else None,
        "worker_alive_at_readback": (
            health.get("worker_alive") if health is not None else None
        ),
        "health_error": health_error,
        "source_error": source_error,
        "post_exit_quote_continuation": (
            health.get("post_exit_quote_continuation")
            if health is not None
            else "not_proved"
        ),
    }
    collection["freshness"] = _freshness(health, as_of=as_of)
    verdict, reason = _verdict(
        collector_configured=collector_configured,
        health=health,
        health_error=health_error,
        source_error=source_error,
        safety=safety,
        cumulative=cumulative_summary,
        freshness=collection["freshness"],
    )
    catalog = _canonical_catalog()
    catalog_hash = catalog["sha256"]
    version = code_version_snapshot()
    armed_count = (
        int(live_envelope_enabled_count)
        if not canary_errors and rollback_latch is None
        else 0
    )
    deployment_state = (
        "disarmed_rollback_latched"
        if rollback_latch is not None
        else "safety_blocked"
        if canary_errors
        else (
            "armed_bounded_canary"
            if armed_count == 1
            else (
                "deployed_canary_disarmed"
                if collector_configured
                else "deployed_not_collecting"
            )
        )
    )
    authorized_canary = None
    if armed_count == 1 and not canary_errors:
        configured = canaries[0]
        safety_hash = next(
            item["policy_hash"]
            for item in catalog["candidates"]
            if item["candidate_id"] == "safety_stack"
        )
        authorized_canary = {
            "authorization_id": configured["authorization_id"],
            "deployment_id": configured["deployment_id"],
            "symbol": configured["symbol"],
            "start_at": configured["start_at"],
            "expires_at": configured["expires_at"],
            "authorized_deployment_id": configured[
                "authorized_deployment_id"
            ],
            "authorized_symbol": configured["authorized_symbol"],
            "authorized_active_plan_id": configured[
                "authorized_active_plan_id"
            ],
            "startup_authorization_fingerprint": configured[
                "startup_authorization_fingerprint"
            ],
            "rollback_action": configured["rollback_action"],
            "candidate_policy_hash": safety_hash,
            "runtime_source_policy_hash": configured[
                "runtime_source_policy_hash"
            ],
            "configured_dte_window": (
                f"{configured['dte_min']}-{configured['dte_max']}"
            ),
            "max_quantity": configured["max_contracts"],
            "max_premium_cap_fraction": configured[
                "max_premium_cap_fraction"
            ],
            "base_max_trade_premium_usd": configured[
                "base_max_trade_premium_usd"
            ],
            "effective_max_trade_premium_usd": configured[
                "effective_max_trade_premium_usd"
            ],
            "new_trades_only": True,
        }
    v2_safety = {
        **safety,
        "shadow_broker_calls": _safety_count(
            health.get("broker_calls_added") if health is not None else 0
        ),
        "unauthorized_live_envelope_count": len(canary_errors),
        "stop_loosen_attempt_count": 0,
        "rollback_latched_count": 1 if rollback_latch is not None else 0,
    }
    verdict = _v2_verdict(verdict)
    if canary_errors or rollback_latch is not None:
        verdict = "safety_blocked"
    if rollback_latch is not None:
        reason = (
            "The configured live canary is disarmed by a durable rollback "
            "latch; new entries and ratchets remain blocked."
        )
    completed_sessions = len(
        {
            str(case.get("cluster_id"))
            for case in cumulative_cases
            if case.get("cluster_id")
        }
    )
    weekly_v2 = _v2_summary(weekly_summary, weekly_cases)
    cumulative_v2 = _v2_summary(cumulative_summary, cumulative_cases)
    verdict, reason = _fail_closed_heterogeneity(
        verdict, reason, weekly_v2, cumulative_v2
    )
    maturity_stage = _maturity_stage(maturity, cumulative=cumulative_v2)
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "report_type": REPORT_TYPE,
        "producer": "bhiksha",
        "artifact_id": f"bhiksha-exit-policy-weekly:{week_end.isoformat()}",
        "through": week_end.isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "scope": "cumulative_observational_collection_as_of_through",
        "experiment_id": RISK_ENVELOPE_EXPERIMENT_ID,
        "experiment_spec_hashes": experiment_spec_hashes,
        "producer_policy_identities": _producer_policy_identities(
            cumulative_cases
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "catalog": catalog,
        "deployment": {
            "state": deployment_state,
            "source_commit": str(version.get("git_commit") or "unknown"),
            "source_dirty": version.get("git_dirty"),
            "catalog_sha256": catalog_hash,
        },
        "arming": {
            "armed_deployment_count": armed_count,
            "armed_candidate_id": (
                "safety_stack" if armed_count == 1 and not canary_errors else None
            ),
            "authorized_canary": authorized_canary,
            "rollback_latch": rollback_latch,
        },
        "collection": collection,
        "weekly": weekly_v2,
        "cumulative": cumulative_v2,
        "maturity_windows": maturity,
        "maturity": {
            "stage": maturity_stage,
            "completed_sessions": completed_sessions,
            "windows": maturity["checkpoints"],
        },
        "safety": v2_safety,
        "verdict": {
            "status": verdict,
            "reason": reason,
            "promotion_status": "not_requested",
        },
        "inference": {
            "eligible": bool(cumulative_summary.get("inference_eligible")),
            "blockers": list(cumulative_summary.get("inference_blockers") or []),
            "decision_ready": False,
            "decision_blockers": [
                "operator_decision_required",
                "candidate_specific_promotion_gate_not_satisfied",
            ],
        },
    }
    evidence["receipt"] = {
        "status": "ok",
        "sha256": stable_digest(evidence),
        "through": week_end.isoformat(),
    }
    return evidence


def stable_digest(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "receipt"}
    }
    collection = stable.get("collection")
    if isinstance(collection, dict) and isinstance(collection.get("freshness"), dict):
        stable["collection"] = dict(collection)
        stable["collection"]["freshness"] = {
            key: value
            for key, value in collection["freshness"].items()
            if key != "age_seconds"
        }
    return hashlib.sha256(
        json.dumps(
            stable,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _read_health(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None, "exit_edge_health_not_an_object"
        return payload, None
    except FileNotFoundError:
        return None, "exit_edge_health_missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}:{exc}"


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") or {}
    return {
        "registration_denominator": dict(
            summary.get("registration_denominator") or _empty_denominator()
        ),
        "case_count": int(summary.get("case_count") or 0),
        "paired_count": int(summary.get("paired_count") or 0),
        "insufficient_count": int(summary.get("insufficient_count") or 0),
        "cluster_count": int(summary.get("cluster_count") or 0),
        "homogeneous_experiment_spec": bool(
            summary.get("homogeneous_experiment_spec", True)
        ),
        "risk_envelope_candidate_vs_control": dict(
            summary.get("risk_envelope_candidate_vs_control") or {}
        ),
        "risk_envelope_missingness": dict(
            summary.get("risk_envelope_missingness") or {}
        ),
        "inference_eligible": bool(summary.get("inference_eligible", False)),
        "inference_blockers": list(summary.get("inference_blockers") or []),
        "confidence": dict(summary.get("confidence") or {}),
    }


def _empty_denominator() -> dict[str, Any]:
    return {
        "confirmed_fill_attempts": None,
        "eligible_attempts": None,
        "registered_cohorts": None,
        "missing_or_ineligible_registrations": None,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "registration_denominator": _empty_denominator(),
        "case_count": 0,
        "paired_count": 0,
        "insufficient_count": 0,
        "cluster_count": 0,
        "homogeneous_experiment_spec": None,
        "risk_envelope_candidate_vs_control": {},
        "risk_envelope_missingness": {},
        "inference_eligible": False,
        "inference_blockers": [],
        "confidence": {},
    }


def _canonical_catalog() -> dict[str, Any]:
    vectors = load_protective_floor_conformance_vectors()
    experiment = vectors["experiment"]
    overlay_hashes = vectors["expected_candidate_overlay_hashes"]
    candidates = [
        {
            "candidate_id": item["candidate_id"],
            "policy_id": item["policy_id"],
            "policy_hash": overlay_hashes[item["candidate_id"]],
        }
        for item in experiment["candidates"]
    ]
    catalog: dict[str, Any] = {
        "catalog_id": experiment["experiment_id"],
        "experiment_schema": experiment["schema_version"],
        "experiment_hash": vectors["expected_experiment_hash"],
        "shared_core_id": experiment["shared_core"]["core_id"],
        "shared_core_hash": vectors["expected_shared_core_hash"],
        "candidates": candidates,
    }
    catalog["sha256"] = hashlib.sha256(
        json.dumps(
            catalog,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return catalog


def _v2_summary(
    summary: dict[str, Any],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    candidates = {
        "control": {
            "paired_count": int(summary.get("paired_count") or 0),
            "total_delta_pnl_usd": 0.0
            if summary.get("paired_count")
            else None,
            "mean_delta_pnl_usd": 0.0
            if summary.get("paired_count")
            else None,
            "candidate_specific_exit_count": 0,
        }
    }
    for candidate_id in SHADOW_CANDIDATE_IDS[1:]:
        candidates[candidate_id] = dict(
            (
                summary.get("risk_envelope_candidate_vs_control") or {}
            ).get(candidate_id)
            or {
                "paired_count": 0,
                "total_delta_pnl_usd": None,
                "mean_delta_pnl_usd": None,
                "candidate_specific_exit_count": 0,
            }
        )
    eligible_count = len(cases)
    terminal_count = sum(case.get("status") == "paired" for case in cases)
    post_exit_complete_count = sum(
        case.get("status") == "paired"
        and not (
            (case.get("missingness") or {}).get(
                "arms_without_post_exit_quote"
            )
            or []
        )
        for case in cases
    )
    return {
        **summary,
        "eligible_observation_count": eligible_count,
        "paired_count": terminal_count,
        "terminal_paired_count": terminal_count,
        "post_exit_complete_trade_count": post_exit_complete_count,
        "right_censored_trade_count": eligible_count - terminal_count,
        "candidates": candidates,
        "cohorts": _cohort_rows(cases),
        "homogeneous_catalog": bool(
            summary.get("homogeneous_experiment_spec", True)
        ),
        "homogeneous_fill_model": True,
        "homogeneous_executable_reference": True,
    }


def _cohort_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for case in cases:
        dimensions = case.get("cohort_dimensions") or {}
        experiment = case.get("experiment_spec") or {}
        arms = ((experiment.get("risk_envelope") or {}).get("arms") or [])
        control = next(
            (
                arm
                for arm in arms
                if arm.get("candidate_id") == "control"
            ),
            {},
        )
        strategy_policy_id = str(
            control.get("candidate_policy_id") or "unknown"
        )
        strategy_policy_hash = str(
            dimensions.get("strategy_policy_hash")
            or control.get("candidate_policy_hash")
            or "unknown"
        )
        configured_dte_min = _cohort_value(
            dimensions, "configured_dte_min"
        )
        configured_dte_max = _cohort_value(
            dimensions, "configured_dte_max"
        )
        authorization_mode = _cohort_value(
            dimensions,
            "authorization_mode",
            legacy_name="authorization",
        )
        key = (
            strategy_policy_id,
            strategy_policy_hash,
            configured_dte_min,
            configured_dte_max,
            _cohort_value(dimensions, "dte_fallback_policy"),
            _cohort_value(dimensions, "runtime_mode"),
            authorization_mode,
            _cohort_value(dimensions, "authorization_id"),
            _present_or_unknown(case.get("symbol")),
            _present_or_unknown(case.get("cluster_id")),
            _dte_bucket(dimensions.get("selected_dte")),
            _delta_bucket(dimensions.get("selected_abs_delta")),
            _liquidity_bucket(
                dimensions.get("entry_spread_pct")
                if dimensions.get("entry_spread_pct") is not None
                else dimensions.get("selected_spread_pct")
            ),
        )
        row = grouped.setdefault(
            key,
            {
                "producer": "bhiksha",
                "strategy_policy_id": key[0],
                "strategy_policy_hash": key[1],
                "configured_dte_min": key[2],
                "configured_dte_max": key[3],
                "dte_fallback_policy": key[4],
                "runtime_mode": key[5],
                "authorization_mode": key[6],
                "authorization_id": key[7],
                "symbol": key[8],
                "cluster_id": key[9],
                "dte_bucket": key[10],
                "delta_bucket": key[11],
                "liquidity_bucket": key[12],
                "paired_count": 0,
                "case_count": 0,
                "unknown_reasons": {},
            },
        )
        source_fields = {
            "strategy_policy_id": "experiment_spec.risk_envelope.arms.control.candidate_policy_id",
            "strategy_policy_hash": "cohort_dimensions.strategy_policy_hash",
            "configured_dte_min": "cohort_dimensions.configured_dte_min",
            "configured_dte_max": "cohort_dimensions.configured_dte_max",
            "dte_fallback_policy": "cohort_dimensions.dte_fallback_policy",
            "runtime_mode": "cohort_dimensions.runtime_mode",
            "authorization_mode": "cohort_dimensions.authorization_mode",
            "symbol": "cohort.symbol",
            "cluster_id": "cohort.cluster_id",
            "dte_bucket": "cohort_dimensions.selected_dte",
            "delta_bucket": "cohort_dimensions.selected_abs_delta",
            "liquidity_bucket": "cohort_dimensions.entry_spread_pct",
        }
        for dimension, source_field in source_fields.items():
            if row.get(dimension) in (None, "", "unknown"):
                row["unknown_reasons"][dimension] = {
                    "code": "source_event_predates_capture",
                    "source_field": source_field,
                }
        row["case_count"] += 1
        row["paired_count"] += case.get("status") == "paired"
    return [
        grouped[key]
        for key in sorted(
            grouped,
            key=lambda item: json.dumps(
                item,
                default=str,
                separators=(",", ":"),
            ),
        )
    ]


def _present_or_unknown(value: Any) -> Any:
    return value if value is not None and value != "" else "unknown"


def _cohort_value(
    dimensions: dict[str, Any],
    name: str,
    *,
    legacy_name: str | None = None,
) -> Any:
    value = dimensions.get(name)
    if value is None and legacy_name is not None:
        value = dimensions.get(legacy_name)
    return _present_or_unknown(value)


def _producer_policy_identities(
    cases: list[dict[str, Any]],
) -> list[dict[str, str]]:
    identities: dict[tuple[str, str, str], dict[str, str]] = {}
    for case in cases:
        experiment = case.get("experiment_spec") or {}
        for arm in (
            (experiment.get("risk_envelope") or {}).get("arms") or []
        ):
            candidate_id = str(arm.get("candidate_id") or "")
            policy_id = str(arm.get("candidate_policy_id") or "")
            policy_hash = str(arm.get("candidate_policy_hash") or "")
            if not candidate_id or not policy_id or not policy_hash:
                continue
            key = (candidate_id, policy_id, policy_hash)
            identities[key] = {
                "candidate_id": candidate_id,
                "producer_policy_id": policy_id,
                "producer_policy_hash": policy_hash,
            }
    return [identities[key] for key in sorted(identities)]


def _dte_bucket(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if number <= 3:
        return "0-3"
    if number <= 7:
        return "4-7"
    if number <= 14:
        return "8-14"
    return "15+"


def _delta_bucket(value: Any) -> str:
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return "unknown"
    if number < 0.25:
        return "<0.25"
    if number < 0.35:
        return "0.25-0.34"
    if number < 0.45:
        return "0.35-0.44"
    return ">=0.45"


def _liquidity_bucket(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if number <= 0.05:
        return "<=5%"
    if number <= 0.10:
        return ">5-10%"
    return ">10%"


def _maturity_stage(
    maturity: dict[str, Any],
    *,
    cumulative: dict[str, Any],
) -> str:
    checkpoints = maturity.get("checkpoints") or {}
    if int((checkpoints.get("W3") or {}).get("mature_cohort_count") or 0):
        terminal = int(cumulative.get("terminal_paired_count") or 0)
        if (
            terminal > 0
            and int(cumulative.get("post_exit_complete_trade_count") or 0)
            >= terminal
            and int(cumulative.get("right_censored_trade_count") or 0) == 0
            and all(
                int(metric.get("paired_count") or 0) == terminal
                for metric in (cumulative.get("candidates") or {}).values()
            )
        ):
            return "week3_economics"
        return "insufficient"
    if int((checkpoints.get("W2") or {}).get("mature_cohort_count") or 0):
        return "week2_behavior"
    if int((checkpoints.get("W1") or {}).get("mature_cohort_count") or 0):
        return "week1_integrity"
    return "insufficient"


def _v2_verdict(value: str) -> str:
    return {
        "collecting_inference_blocked": "insufficient",
        "historical_cutoff_unverifiable": "unavailable",
        "insufficient_cluster_sample": "insufficient",
        "directional_profile_uplift": "directional",
        "live_collection_inference_blocked": "insufficient",
    }.get(value, value if value in {
        "unavailable",
        "not_collecting",
        "awaiting_first_collection",
        "stale_collection",
        "collection_unreadable",
        "week1_integrity",
        "week2_behavior",
        "week3_economics",
        "insufficient",
        "inconclusive",
        "directional",
        "review_ready",
        "heterogeneous_specs",
        "safety_blocked",
    } else "inconclusive")


def _fail_closed_heterogeneity(
    verdict: str,
    reason: str,
    *summaries: dict[str, Any],
) -> tuple[str, str]:
    homogeneous = all(
        summary[dimension]
        for summary in summaries
        for dimension in (
            "homogeneous_catalog",
            "homogeneous_fill_model",
            "homogeneous_executable_reference",
        )
    )
    if homogeneous or verdict in {
        "unavailable",
        "not_collecting",
        "awaiting_first_collection",
        "stale_collection",
        "collection_unreadable",
        "safety_blocked",
    }:
        return verdict, reason
    return (
        "heterogeneous_specs",
        "The collected cohorts do not share one experiment catalog, fill "
        "model, and executable reference; economics remain non-comparable.",
    )


def _safety_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1
    if value < 0 or int(value) != value:
        return 1
    return int(value)


def _empty_maturity(*, as_of: str | None) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "registered_cohorts": 0,
        "first_entry_at": None,
        "last_entry_at": None,
        "checkpoints": {
            label: {
                "minimum_age_days": days,
                "mature_cohort_count": 0,
                "evidence_status": "unavailable",
            }
            for label, days in (("W1", 7), ("W2", 14), ("W3", 21))
        },
        "promotion_verdict": "insufficient_evidence",
    }


def _maturity_summary(raw: dict[str, Any]) -> dict[str, Any]:
    maturity = {
        "as_of": raw.get("as_of"),
        "registered_cohorts": int(raw.get("registered_cohorts") or 0),
        "first_entry_at": raw.get("first_entry_at"),
        "last_entry_at": raw.get("last_entry_at"),
        "checkpoints": {},
        "promotion_verdict": "insufficient_evidence",
    }
    for label, days in (("W1", 7), ("W2", 14), ("W3", 21)):
        source = (raw.get("checkpoints") or {}).get(label) or {}
        count = int(source.get("mature_cohort_count") or 0)
        maturity["checkpoints"][label] = {
            "minimum_age_days": days,
            "mature_cohort_count": count,
            "evidence_status": (
                "mature_observations_available"
                if count > 0
                else "insufficient_evidence"
            ),
        }
    return maturity


def _canary_errors(
    *,
    enabled_count: int,
    canaries: list[dict[str, Any]],
    as_of: datetime,
) -> list[str]:
    errors: list[str] = []
    if enabled_count != len(canaries):
        errors.append("enabled_count_does_not_match_authorized_manifest")
    if enabled_count > 1:
        errors.append("more_than_one_live_canary")
    for canary in canaries:
        if canary.get("candidate_id") != "safety_stack":
            errors.append("candidate_not_safety_stack")
        expected_overlay_hash = (
            load_protective_floor_conformance_vectors()[
                "expected_candidate_overlay_hashes"
            ]["safety_stack"]
        )
        if canary.get("candidate_overlay_hash") != expected_overlay_hash:
            errors.append("candidate_overlay_hash_mismatch")
        if not str(canary.get("runtime_source_policy_hash") or "").strip():
            errors.append("runtime_source_policy_hash_missing")
        if canary.get("runtime_mode") != "live_approval_gated":
            errors.append("runtime_mode_not_live_approval_gated")
        if canary.get("dte_fallback_policy") != "strict":
            errors.append("dte_fallback_not_strict")
        if canary.get("dte_min") != 4:
            errors.append("dte_min_not_4")
        if canary.get("dte_max") != 7:
            errors.append("dte_max_not_7")
        if canary.get("max_contracts") != 1:
            errors.append("max_contracts_not_1")
        if not str(canary.get("authorization_id") or "").strip():
            errors.append("authorization_id_missing")
        if (
            canary.get("deployment_id")
            != "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
            or canary.get("authorized_deployment_id")
            != canary.get("deployment_id")
            or str(canary.get("symbol") or "").upper() != "IWM"
            or str(canary.get("authorized_symbol") or "").upper() != "IWM"
        ):
            errors.append("increment_2_iwm_authority_mismatch")
        for field in (
            "start_at",
            "expires_at",
            "authorized_active_plan_id",
            "startup_authorization_fingerprint",
        ):
            if not str(canary.get(field) or "").strip():
                errors.append(f"{field}_missing")
        try:
            starts_at = _aware_datetime(canary.get("start_at"))
            expires_at = _aware_datetime(canary.get("expires_at"))
            if starts_at > as_of:
                errors.append("authorization_not_yet_valid")
            if expires_at <= as_of:
                errors.append("authorization_expired")
            if starts_at >= expires_at:
                errors.append("authorization_window_invalid")
        except (TypeError, ValueError):
            errors.append("authorization_window_invalid")
        if canary.get("rollback_action") != "disable_canary_restore_control":
            errors.append("rollback_action_invalid")
        cap_fraction = canary.get("max_premium_cap_fraction")
        if (
            isinstance(cap_fraction, bool)
            or not isinstance(cap_fraction, (int, float))
            or not 0 < float(cap_fraction) <= 0.20
        ):
            errors.append("premium_cap_fraction_out_of_bounds")
        if canary.get("base_max_trade_premium_usd") != 2_000.0:
            errors.append("base_max_trade_premium_not_2000")
        if canary.get("effective_max_trade_premium_usd") != 400.0:
            errors.append("effective_max_trade_premium_not_400")
    return sorted(set(errors))


def _aware_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("authorization timestamps must carry timezone")
    return parsed.astimezone(UTC)


def _verdict(
    *,
    collector_configured: bool,
    health: dict[str, Any] | None,
    health_error: str | None,
    source_error: str | None,
    safety: dict[str, Any],
    cumulative: dict[str, Any],
    freshness: dict[str, Any],
) -> tuple[str, str]:
    if not safety["canary_contract_valid"]:
        return (
            "safety_blocked",
            "The bounded live-canary manifest is missing or violates its safety contract.",
        )
    if not collector_configured:
        return (
            "not_collecting",
            "The persistent scheduled-context Exit Edge collector is not enabled.",
        )
    if health is None:
        return (
            "awaiting_first_collection",
            f"The collector is configured but has no readable health receipt: {health_error}.",
        )
    if (
        safety["collector_enforcement_authority"] is not False
        or safety["broker_calls_added"] != 0
    ):
        return (
            "safety_blocked",
            "The collector health receipt does not prove zero enforcement authority and zero broker calls.",
        )
    if health.get("enabled") is not True:
        return (
            "collection_unreadable",
            "The collector is configured, but its health receipt does not report enabled=true.",
        )
    if (
        "registration_failures" not in health
        or health.get("registration_failures") != 0
    ):
        return (
            "collection_unreadable",
            "The collector health receipt is missing registration-failure proof or reports failures.",
        )
    if freshness["status"] == "unreadable":
        return (
            "collection_unreadable",
            "The collector health receipt has no valid timezone-aware updated_at.",
        )
    if freshness["status"] == "after_cutoff":
        return (
            "historical_cutoff_unverifiable",
            "The available collector health receipt was written after this reporting cutoff.",
        )
    if freshness["status"] == "stale":
        return (
            "stale_collection",
            "The most recent collector health receipt is more than 12 hours old.",
        )
    if source_error is not None:
        return (
            "collection_unreadable",
            f"The current Exit Edge repository could not be analyzed: {source_error}.",
        )
    if not cumulative["inference_eligible"]:
        blockers = ", ".join(cumulative["inference_blockers"]) or "insufficient paired evidence"
        return (
            "collecting_inference_blocked",
            f"Collection is current, but uplift inference is blocked: {blockers}.",
        )
    indicator = str(cumulative["confidence"].get("indicator") or "inconclusive")
    reason = str(
        cumulative["confidence"].get("reason")
        or "The paired evidence has no stronger confidence classification."
    )
    return indicator, reason


def _freshness(
    health: dict[str, Any] | None,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    if health is None:
        return {
            "status": "missing",
            "observed_at": None,
            "age_seconds": None,
            "max_age_seconds": int(MAX_HEALTH_AGE.total_seconds()),
        }
    try:
        observed = datetime.fromisoformat(
            str(health.get("updated_at") or "").replace("Z", "+00:00")
        )
        if observed.tzinfo is None:
            raise ValueError("health timestamp is timezone-naive")
        observed = observed.astimezone(UTC)
    except (TypeError, ValueError):
        return {
            "status": "unreadable",
            "observed_at": health.get("updated_at"),
            "age_seconds": None,
            "max_age_seconds": int(MAX_HEALTH_AGE.total_seconds()),
        }
    age = as_of - observed
    if age < -timedelta(minutes=5):
        status = "after_cutoff"
    elif age > MAX_HEALTH_AGE:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "observed_at": observed.isoformat(),
        "age_seconds": max(int(age.total_seconds()), 0),
        "max_age_seconds": int(MAX_HEALTH_AGE.total_seconds()),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "ExitEdgeWeeklyEvidenceWriteResult",
    "SCHEMA",
    "build_exit_edge_weekly_evidence",
    "stable_digest",
    "write_exit_edge_weekly_evidence",
]
