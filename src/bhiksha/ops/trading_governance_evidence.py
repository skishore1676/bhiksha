"""Deterministic promotion and demotion evidence for TradeLab review.

Bhiksha owns these facts because it owns both the trade ledger and Rail B's
persisted demotion state.  The packet is read-only and carries no mechanism to
promote, re-promote, pause, compile, or submit an order.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from bhiksha.risk.demotion_store import DemotionStore

if TYPE_CHECKING:
    from bhiksha.config.models import DeploymentManifest


SCHEMA = "bhiksha.trading_governance_evidence.v1"


def build_trading_governance_evidence(
    scorecard: dict[str, Any],
    *,
    through: date | str,
    deployments: list["DeploymentManifest"] | None = None,
    demotion_store_path: str | Path | None = None,
) -> dict[str, Any]:
    """Join persisted enforcement facts with scorecard review facts."""
    through_text = through.isoformat() if isinstance(through, date) else str(through)
    deployment_meta = _deployment_metadata(deployments)
    records = DemotionStore(demotion_store_path).load()
    active_demotions = []
    for deployment_id, record in sorted(records.items()):
        metadata = deployment_meta.get(deployment_id, {})
        active_demotions.append(
            {
                **record.to_dict(),
                "symbol": metadata.get("symbol"),
                "strategy_key": metadata.get("strategy_key"),
                "mode": "live",
                "enforcement_status": "demoted_to_shadow",
                "matching_shadow_deployments": _matching_shadow_deployments(
                    metadata, deployment_meta
                ),
            }
        )

    promotion = scorecard.get("promotion_candidates") or {}
    named_promotion_lanes = {
        str(item.get("deployment_id") or "")
        for key in ("candidates", "near_misses")
        for item in (promotion.get(key) or [])
    }
    min_closed = int((promotion.get("criteria") or {}).get("min_closed_trades") or 0)
    observing = [
        {
            "deployment_id": lane.get("deployment_id"),
            "display_id": lane.get("display_id"),
            "closed": lane.get("closed", 0),
            "wins": lane.get("wins", 0),
            "total_pnl_usd": lane.get("total_pnl_usd", 0.0),
            "avg_return_pct": lane.get("avg_return_pct"),
            "evidence_gates_relaxed": lane.get("evidence_gates_relaxed"),
            "disqualified_by": "insufficient_closed_trades",
            "closed_trades_needed": max(0, min_closed - int(lane.get("closed") or 0)),
        }
        for lane in (scorecard.get("lanes") or [])
        if lane.get("mode") == "shadow"
        and str(lane.get("deployment_id") or "") not in named_promotion_lanes
    ]
    observing.sort(key=lambda lane: (-int(lane.get("closed") or 0), str(lane.get("deployment_id") or "")))
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "through": through_text,
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {
            "demotions": "bhiksha.risk.DemotionStore",
            "promotions": "bhiksha.ops.weekly_scorecard",
        },
        "active_demotions": active_demotions,
        "promotion_review": {
            "criteria": promotion.get("criteria") or {},
            "candidates": promotion.get("candidates") or [],
            "near_misses": promotion.get("near_misses") or [],
            "observing": observing,
        },
        "data_quality_warnings": scorecard.get("data_quality_warnings") or [],
        "guardrails": {
            "advisory_only": True,
            "automatic_promotion": False,
            "automatic_repromotion": False,
            "operator_decision_required": True,
        },
    }
    body["receipt"] = evidence_receipt(body)
    return body


def evidence_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a stable digest that excludes run time and the receipt itself."""
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "receipt"}
    }
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return {
        "status": "ok",
        "sha256": digest,
        "through": str(payload.get("through") or ""),
        "active_demotion_count": len(payload.get("active_demotions") or []),
        "promotion_candidate_count": len(
            ((payload.get("promotion_review") or {}).get("candidates") or [])
        ),
        "promotion_observing_count": len(
            ((payload.get("promotion_review") or {}).get("observing") or [])
        ),
    }


def _deployment_metadata(
    deployments: list["DeploymentManifest"] | None,
) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for deployment in deployments or []:
        execution = getattr(deployment, "execution", None)
        strategy = getattr(deployment, "strategy", None)
        deployment_id = str(getattr(deployment, "deployment_id", ""))
        if not deployment_id:
            continue
        metadata[deployment_id] = {
            "symbol": getattr(deployment, "symbol", None),
            "strategy_key": getattr(strategy, "key", None),
            "shadow_only": bool(getattr(execution, "shadow_only", False)),
        }
    return metadata


def _matching_shadow_deployments(
    live: dict[str, Any],
    deployments: dict[str, dict[str, Any]],
) -> list[str]:
    if not live:
        return []
    return sorted(
        deployment_id
        for deployment_id, candidate in deployments.items()
        if candidate.get("shadow_only")
        and candidate.get("symbol") == live.get("symbol")
        and candidate.get("strategy_key") == live.get("strategy_key")
    )
