"""Deterministic promotion evidence and risk-authority facts for review.

The Google Sheet is the sole persistent LIVE/SHADOW authority. Rail B may veto
an entry for the current session and records that in Bhiksha events; this
packet has no competing mode override and carries no action surface.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from typing import Any


SCHEMA = "bhiksha.trading_governance_evidence.v1"


def build_trading_governance_evidence(
    scorecard: dict[str, Any],
    *,
    through: date | str,
) -> dict[str, Any]:
    """Build advisory promotion evidence without a persistent mode override."""
    through_text = through.isoformat() if isinstance(through, date) else str(through)
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
            "session_blocks": "bhiksha.events:risk_manager_session_block",
            "promotions": "bhiksha.ops.weekly_scorecard",
        },
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
            "persistent_mode_overrides": False,
            "rail_b_scope": "current_session_entry_veto",
            "sheet_is_mode_authority": True,
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
        "promotion_candidate_count": len(
            ((payload.get("promotion_review") or {}).get("candidates") or [])
        ),
        "promotion_observing_count": len(
            ((payload.get("promotion_review") or {}).get("observing") or [])
        ),
    }
