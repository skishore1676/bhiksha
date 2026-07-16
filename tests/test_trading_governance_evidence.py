from datetime import UTC, datetime
import json
from types import SimpleNamespace

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


def test_evidence_surfaces_repromotion_reset(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    store = DemotionStore(path)
    store.record_demotion(
        deployment_id="iwm-live",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-27.94,
        threshold_usd=0.0,
        trade_ids=["t1"],
        now=datetime(2026, 7, 16, 13, 53, tzinfo=UTC),
    )
    store.repromote_many(
        ["iwm-live"],
        reason="operator fresh trial",
        approved_by="suman",
        now=datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
    )

    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", demotion_store_path=path
    )

    assert evidence["active_demotions"] == []
    assert evidence["repromotion_resets"][0]["deployment_id"] == "iwm-live"
    assert evidence["repromotion_resets"][0]["prior_demotion"]["mean_pnl_usd"] == -27.94
    assert evidence["receipt"]["repromotion_reset_count"] == 1


def test_evidence_marks_reset_historical_after_fresh_redemotion(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    store = DemotionStore(path)
    store.record_demotion(
        deployment_id="iwm-live",
        reason="first_window_negative",
        window_n=10,
        mean_pnl_usd=-27.94,
        threshold_usd=0.0,
        trade_ids=["old"],
    )
    store.repromote_many(
        ["iwm-live"], reason="fresh trial", approved_by="suman"
    )
    store.record_demotion(
        deployment_id="iwm-live",
        reason="fresh_window_negative",
        window_n=10,
        mean_pnl_usd=-12.0,
        threshold_usd=0.0,
        trade_ids=["new"],
    )

    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", demotion_store_path=path
    )

    assert evidence["active_demotions"][0]["enforcement_status"] == "demoted_to_shadow"
    assert evidence["repromotion_resets"][0]["enforcement_status"] == "historical_repromotion_reset"
    assert evidence["repromotion_resets"][0]["is_current_cutoff"] is False


def test_demotion_does_not_name_itself_as_matching_shadow(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    DemotionStore(path).record_demotion(
        deployment_id="qqq-live",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-10.0,
        threshold_usd=0.0,
        trade_ids=["t1"],
    )
    deployments = [
        SimpleNamespace(
            deployment_id="qqq-live",
            symbol="QQQ",
            strategy=SimpleNamespace(key="market_impulse"),
            execution=SimpleNamespace(shadow_only=True),
        ),
        SimpleNamespace(
            deployment_id="qqq-shadow",
            symbol="QQQ",
            strategy=SimpleNamespace(key="market_impulse"),
            execution=SimpleNamespace(shadow_only=True),
        ),
    ]

    evidence = build_trading_governance_evidence(
        _scorecard(), through="2026-07-16", deployments=deployments, demotion_store_path=path
    )

    assert evidence["active_demotions"][0]["matching_shadow_deployments"] == ["qqq-shadow"]
