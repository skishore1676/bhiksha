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
    analyze_prospective_repository,
)


SCHEMA = "bhiksha.exit_edge_weekly_evidence.v1"
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
) -> ExitEdgeWeeklyEvidenceWriteResult:
    evidence = build_exit_edge_weekly_evidence(
        db_path=db_path,
        status_path=status_path,
        week_start=week_start,
        week_end=week_end,
        collector_configured=collector_configured,
        live_envelope_enabled_count=live_envelope_enabled_count,
    )
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"exit_edge_weekly_evidence_{week_end.isoformat()}.json"
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
) -> dict[str, Any]:
    if week_start > week_end:
        raise ValueError("week_start must be on or before week_end")

    db = Path(db_path)
    status = Path(status_path)
    health, health_error = _read_health(status)
    source_error: str | None = None
    weekly_summary = _empty_summary()
    cumulative_summary = _empty_summary()
    experiment_spec_hashes: list[str] = []
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
            experiment_spec_hashes = list(
                cumulative_report.get("experiment_spec_hashes") or []
            )
        except (OSError, ValueError, TypeError) as exc:
            source_error = f"{type(exc).__name__}:{exc}"
        except Exception as exc:  # sqlite errors remain explicit evidence failures.
            source_error = f"{type(exc).__name__}:{exc}"
    else:
        source_error = "exit_edge_database_missing"

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
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "report_type": "prospective_live_repository_weekly_readback",
        "artifact_id": f"bhiksha-exit-edge-weekly:{week_end.isoformat()}",
        "through": week_end.isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "scope": "cumulative_observational_collection_as_of_through",
        "experiment_id": RISK_ENVELOPE_EXPERIMENT_ID,
        "experiment_spec_hashes": experiment_spec_hashes,
        "generated_at": datetime.now(UTC).isoformat(),
        "collection": collection,
        "weekly": weekly_summary,
        "cumulative": cumulative_summary,
        "safety": safety,
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
                "candidate_specific_promotion_gate_not_defined",
                "increment_2_live_activation_unapproved",
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
        "case_count": None,
        "paired_count": None,
        "insufficient_count": None,
        "cluster_count": None,
        "homogeneous_experiment_spec": None,
        "risk_envelope_candidate_vs_control": {},
        "risk_envelope_missingness": {},
        "inference_eligible": False,
        "inference_blockers": [],
        "confidence": {},
    }


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
    if int(safety["live_envelope_enabled_count"]) != 0:
        return (
            "safety_blocked",
            "The live-envelope zero-activation invariant is violated.",
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
