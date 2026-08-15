from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    compile_active_plan_from_rows,
)
from bhiksha.cartographer_profiles import profile_bundle
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.state.position_tracker import TrackedPosition


class _Events:
    def __init__(self) -> None:
        self.rows = []

    async def append(self, event_type, payload) -> None:
        self.rows.append((event_type, payload))


def _row() -> ActivePlanSheetRow:
    bundle = profile_bundle("TREND_CONTINUATION")
    return ActivePlanSheetRow.model_validate(
        {
            "row_id": "mc-v1-example",
            "row_type": "manual",
            "manual_setup_type": "manual_trigger",
            "enabled": True,
            "authorization_mode": "shadow",
            "symbol": "SPY",
            "direction": "long",
            "trigger_price": 600.0,
            "trigger_direction": "ABOVE",
            "after_time_et": "09:35",
            "end_in_days": bundle["execution"]["dte_max"],
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


def _compile(tmp_path: Path, row: ActivePlanSheetRow):
    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    return compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog,
        trading_date="2026-08-17",
        operator_defaults={"max_trade_premium_usd": 400.0},
    )


def test_cartographer_bundle_compiles_only_the_registry_values(tmp_path: Path) -> None:
    compiled = _compile(tmp_path, _row())
    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.dte_min == 3
    assert deployment.execution.dte_max == 7
    assert deployment.execution.dte_fallback_policy == "strict"
    assert deployment.risk.max_trade_premium_usd == 400.0
    assert deployment.risk.max_contracts == 1
    metadata = deployment.source.metadata
    assert metadata["requested_max_trade_premium_usd"] == 500.0
    assert metadata["operator_max_trade_premium_usd"] == 400.0
    assert metadata["effective_max_trade_premium_usd"] == 400.0


def test_cartographer_mismatch_and_stale_rows_fail_closed(tmp_path: Path) -> None:
    mismatch = _row().model_copy(deep=True)
    mismatch.execution_overrides["dte_max"] = 8
    compiled = _compile(tmp_path, mismatch)
    assert "execution specification" in compiled.plan.suppressed[0]["reason"]

    stale = _row().model_copy(deep=True)
    stale.source_metadata["trading_date"] = "2026-08-16"
    compiled = _compile(tmp_path, stale)
    assert "stale" in compiled.plan.suppressed[0]["reason"]


def test_cartographer_sheet_risk_cannot_raise_operator_ceiling(tmp_path: Path) -> None:
    row = _row().model_copy(deep=True)
    row.risk_overrides["operator_max_trade_premium_usd"] = 900.0
    row.risk_overrides["effective_max_trade_premium_usd"] = 500.0
    compiled = _compile(tmp_path, row)
    assert "authoritative operator ceiling" in compiled.plan.suppressed[0]["reason"]


@pytest.mark.asyncio
async def test_terminal_owner_fact_keeps_valid_shadow_gross_pnl(tmp_path: Path) -> None:
    deployment = _compile(tmp_path, _row()).plan.deployments[0]
    events = _Events()
    supervisor = object.__new__(ExecutionSupervisor)
    supervisor.event_repository = events
    position = TrackedPosition(
        symbol="SPY", deployment_id=deployment.deployment_id, trade_id="trade-1",
        option_symbol="SPY_CALL", quantity=2, entry_price=1.0,
        entry_timestamp=datetime(2026, 8, 17, 15, tzinfo=UTC), source="shadow",
    )
    await supervisor._record_cartographer_terminal_fact(
        deployment, position, terminal_reason="profile_exit",
        fill_details={"exit_price": 1.25},
    )
    event_type, fact = events.rows[0]
    assert event_type == "cartographer_terminal_fact"
    assert fact["gross_pnl_usd"] == 50.0
    assert fact["decision_ready"] is False
