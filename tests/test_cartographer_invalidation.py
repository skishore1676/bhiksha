from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from bhiksha.active_plan.compiler import ActivePlanSheetRow, compile_active_plan_from_rows
from bhiksha.cartographer_profiles import profile_bundle
from bhiksha.execution.cartographer_invalidation import entry_guard, evaluate_invalidation
from bhiksha.state.position_tracker import TrackedPosition


def _row() -> ActivePlanSheetRow:
    bundle = profile_bundle("TREND_CONTINUATION")
    return ActivePlanSheetRow.model_validate(
        {
            "row_id": "mc-v1-example",
            "row_type": "manual",
            "manual_setup_type": "manual_trigger",
            "authorization_mode": "shadow",
            "symbol": "SPY",
            "direction": "long",
            "trigger_price": 600.0,
            "trigger_direction": "ABOVE",
            "end_in_days": 7,
            "execution_overrides": bundle["execution"],
            "exit_profile_spec": bundle["management"],
            "risk_overrides": {
                "requested_max_trade_premium_usd": 500.0,
                "operator_max_trade_premium_usd": 400.0,
                "effective_max_trade_premium_usd": 400.0,
            },
            "source_metadata": {
                "source_owner": "market_cartographer",
                "signal_id": "mc-v1-example",
                "signal_hash": "sha256:example",
                "cartographer_version": "1.0",
                "run_id": "run-example",
                "trading_date": "2026-08-17",
                "valid_through": "2026-08-17T15:00:00-05:00",
                "profile_slug": "TREND_CONTINUATION",
                "bundle_hash": bundle["bundle_hash"],
                "invalidation_price": 590.0,
            },
        }
    )


def _compile(tmp_path, row: ActivePlanSheetRow):
    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    return compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog,
        trading_date="2026-08-17",
        operator_defaults={"max_trade_premium_usd": 400.0},
    )


def test_cartographer_entry_guard_requires_fresh_unexpired_observation(tmp_path) -> None:
    deployment = _compile(tmp_path, _row()).plan.deployments[0]
    metadata = deployment.source.metadata
    observed_at = datetime.now(UTC)
    assert entry_guard(metadata, direction="long", close=600.0, observed_at=observed_at) is None
    assert (
        entry_guard(
            metadata,
            direction="long",
            close=600.0,
            observed_at=observed_at - timedelta(seconds=61),
            now=observed_at,
        )
        == "chart_entry_observation_stale"
    )


def test_chart_invalidation_preempts_other_exit_management(tmp_path) -> None:
    deployment = _compile(tmp_path, _row()).plan.deployments[0]
    frame = pl.DataFrame(
        {"timestamp": [datetime(2026, 8, 17, 15, 0, tzinfo=UTC)], "close": [590.0]}
    )
    decision = evaluate_invalidation(
        deployment,
        frame,
        TrackedPosition(symbol="SPY", deployment_id=deployment.deployment_id, quantity=1),
    )
    assert decision is not None
    assert decision.exit is True
    assert decision.reason == ["chart_invalidation_underlying"]


def test_short_high_breach_is_chart_invalidation(tmp_path) -> None:
    row = _row().model_copy(deep=True)
    row.direction = "short"
    row.trigger_price = 600.0
    row.source_metadata["invalidation_price"] = 610.0
    deployment = _compile(tmp_path, row).plan.deployments[0]
    frame = pl.DataFrame(
        {"timestamp": [datetime(2026, 8, 17, 15, 0, tzinfo=UTC)], "close": [610.0]}
    )
    assert evaluate_invalidation(
        deployment,
        frame,
        TrackedPosition(symbol="SPY", deployment_id=deployment.deployment_id, quantity=1),
    ).reason == ["chart_invalidation_underlying"]
