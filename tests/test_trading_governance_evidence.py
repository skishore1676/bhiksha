import json

from bhiksha.ops.trading_governance_evidence import (
    build_trading_governance_evidence,
    evidence_receipt,
)


def _scorecard() -> dict:
    return {
        "promotion_candidates": {
            "criteria": {"mode": "shadow", "min_closed_trades": 5},
            "candidates": [{"deployment_id": "strong-shadow", "closed": 7}],
            "near_misses": [{"deployment_id": "weak-shadow", "closed": 6}],
        },
        "data_quality_warnings": [{"code": "example"}],
        "lanes": [
            {
                "deployment_id": "young-shadow",
                "display_id": "young-shadow",
                "mode": "shadow",
                "closed": 3,
                "wins": 2,
                "total_pnl_usd": 25.0,
                "avg_return_pct": 4.0,
                "evidence_gates_relaxed": [],
            }
        ],
    }


def test_evidence_preserves_advisory_review_without_mode_override() -> None:
    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16"
    )

    assert evidence["schema"] == "bhiksha.trading_governance_evidence.v1"
    assert "active_demotions" not in evidence
    assert "repromotion_resets" not in evidence
    assert evidence["promotion_review"]["candidates"][0]["deployment_id"] == "strong-shadow"
    assert evidence["promotion_review"]["observing"][0]["deployment_id"] == "young-shadow"
    assert evidence["promotion_review"]["observing"][0]["closed_trades_needed"] == 2
    assert evidence["guardrails"]["automatic_promotion"] is False
    assert evidence["guardrails"]["persistent_mode_overrides"] is False
    assert evidence["guardrails"]["rail_b_scope"] == "current_session_entry_veto"
    assert evidence["guardrails"]["sheet_is_mode_authority"] is True
    assert "action" not in json.dumps(evidence).lower()


def test_evidence_receipt_is_stable_across_generated_time() -> None:
    first = build_trading_governance_evidence(_scorecard(), through="2026-07-16")
    second = {**first, "generated_at": "2099-01-01T00:00:00+00:00"}
    second["receipt"] = evidence_receipt(second)

    assert second["receipt"]["sha256"] == first["receipt"]["sha256"]


def test_evidence_receipt_detects_fact_change() -> None:
    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16"
    )
    evidence["promotion_review"]["candidates"][0]["closed"] = 8

    assert evidence_receipt(evidence)["sha256"] != evidence["receipt"]["sha256"]
