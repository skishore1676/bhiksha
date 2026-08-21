from __future__ import annotations

from bhiksha.experiments.cartographer_shadow import build_terminal_fact


def _deployment() -> dict[str, object]:
    return {
        "deployment_id": "mc-v1-example",
        "source": {"metadata": {
            "source_owner": "market_cartographer", "signal_id": "mc-v1-example", "signal_hash": "sha256:signal",
            "cartographer_version": "1.0", "run_id": "run-1", "profile_slug": "TREND_CONTINUATION", "bundle_hash": "sha256:bundle",
        }},
    }


def test_terminal_fact_preserves_physical_close_independent_of_coverage() -> None:
    complete = {"coverage": "complete", "mfe_pct": 0.2, "mae_pct": -0.1}
    fact = build_terminal_fact(
        deployment=_deployment(), trade_id="trade-1", terminal_reason="chart_invalidation_underlying",
        option_excursion=complete, underlying_excursion=complete, gross_pnl_usd=-10,
    )
    assert fact["schema"] == "bhiksha.cartographer_shadow_terminal_fact.v2"
    assert fact["status"] == "closed"
    assert fact["identity"]["signal_id"] == fact["identity"]["deployment_id"]
    assert fact["exit_reason"] == "chart_invalidation_underlying"
    assert fact["option_mfe_pct"] == 0.2
    assert fact["option_mae_pct"] == -0.1
    assert "net_pnl_usd" not in fact
    assert "decision_ready" not in fact
    assert fact["fact_receipt_id"].startswith("sha256:")
    incomplete = build_terminal_fact(
        deployment=_deployment(), trade_id="trade-1", terminal_reason="profile_exit",
        option_excursion={"coverage": "partial", "coverage_reasons": ["option_mark_gap"]},
        underlying_excursion=complete,
        gross_pnl_usd=5,
    )
    assert incomplete["status"] == "closed"
    assert incomplete["gross_pnl_usd"] == 5
    assert incomplete["option_mfe_pct"] is None
    assert incomplete["coverage"]["option"] == {
        "status": "partial",
        "reasons": ["option_mark_gap"],
    }
