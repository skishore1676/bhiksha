from datetime import UTC, datetime
import json

from bhiksha.ops.trading_governance_evidence import (
    build_trading_governance_evidence,
    evidence_receipt,
)
from bhiksha.risk.demotion_store import DemotionStore


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


def test_evidence_joins_demotion_and_promotion_without_action_surface(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    DemotionStore(path).record_demotion(
        deployment_id="qqq-live",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-103.69,
        threshold_usd=0.0,
        trade_ids=["t1", "t2"],
        now=datetime(2026, 7, 16, 13, 53, tzinfo=UTC),
    )

    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", demotion_store_path=path
    )

    assert evidence["schema"] == "bhiksha.trading_governance_evidence.v1"
    assert evidence["active_demotions"][0]["deployment_id"] == "qqq-live"
    assert evidence["active_demotions"][0]["mean_pnl_usd"] == -103.69
    assert evidence["promotion_review"]["candidates"][0]["deployment_id"] == "strong-shadow"
    assert evidence["promotion_review"]["observing"][0]["deployment_id"] == "young-shadow"
    assert evidence["promotion_review"]["observing"][0]["closed_trades_needed"] == 2
    assert evidence["guardrails"]["automatic_promotion"] is False
    assert evidence["guardrails"]["automatic_repromotion"] is False
    assert "action" not in json.dumps(evidence).lower()


def test_evidence_receipt_is_stable_across_generated_time(tmp_path) -> None:
    first = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", demotion_store_path=tmp_path / "none.json"
    )
    second = {**first, "generated_at": "2099-01-01T00:00:00+00:00"}
    second["receipt"] = evidence_receipt(second)

    assert second["receipt"]["sha256"] == first["receipt"]["sha256"]


def test_evidence_receipt_detects_fact_change(tmp_path) -> None:
    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", demotion_store_path=tmp_path / "none.json"
    )
    evidence["promotion_review"]["candidates"][0]["closed"] = 8

    assert evidence_receipt(evidence)["sha256"] != evidence["receipt"]["sha256"]
