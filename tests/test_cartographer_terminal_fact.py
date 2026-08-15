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


def test_terminal_fact_preserves_identity_and_coverage_blocker() -> None:
    complete = {"coverage": "complete", "mfe_pct": 0.2, "mae_pct": -0.1}
    fact = build_terminal_fact(
        deployment=_deployment(), trade_id="trade-1", terminal_reason="chart_invalidation_underlying",
        option_excursion=complete, underlying_excursion=complete, gross_pnl_usd=-10, net_pnl_usd=-12,
    )
    assert fact["status"] == "closed"
    assert fact["identity"]["signal_id"] == fact["identity"]["deployment_id"]
    assert fact["fact_receipt_id"].startswith("sha256:")
    incomplete = build_terminal_fact(
        deployment=_deployment(), trade_id="trade-1", terminal_reason="profile_exit",
        option_excursion={"coverage": "partial"}, underlying_excursion=complete, gross_pnl_usd=5, net_pnl_usd=4,
    )
    assert incomplete["status"] == "inconclusive"
    assert incomplete["gross_pnl_usd"] is None
