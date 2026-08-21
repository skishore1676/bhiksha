from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    compile_active_plan_from_rows,
    load_operator_defaults_sheet_rows,
)
from bhiksha.cartographer_profiles import profile_bundle
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.state.position_tracker import TrackedPosition


class _Events:
    def __init__(self) -> None:
        self.rows = []

    async def append(self, event_type, payload) -> None:
        self.rows.append((event_type, payload))


def _operator_defaults(ceiling: float | str = 400.0) -> dict:
    return {
        "max_trade_premium_usd": ceiling,
        "dte_fallback_policy": "allow_nearest_after",
        "delta_min": 0.15,
        "delta_max": 0.35,
        "min_open_interest": 100,
        "max_bid_ask_spread_pct": 0.20,
        "max_contracts": 1,
        "profile__trend_continuation": {
            "dte_min": 3,
            "dte_max": 7,
            "max_trade_premium_usd": 500.0,
            "max_contracts": 1,
        },
    }


def _row(operator_defaults: dict | None = None) -> ActivePlanSheetRow:
    defaults = operator_defaults or _operator_defaults()
    bundle = profile_bundle("TREND_CONTINUATION", defaults)
    operator_ceiling = float(defaults["max_trade_premium_usd"])
    requested_ceiling = float(bundle["requested_max_trade_premium_usd"])
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
                "requested_max_trade_premium_usd": requested_ceiling,
                "operator_max_trade_premium_usd": operator_ceiling,
                "effective_max_trade_premium_usd": min(
                    requested_ceiling, operator_ceiling
                ),
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


def _compile(
    tmp_path: Path, row: ActivePlanSheetRow, operator_defaults: dict | None = None
):
    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    return compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog,
        trading_date="2026-08-17",
        operator_defaults=operator_defaults or _operator_defaults(),
    )


def test_cartographer_compiles_only_the_sheet_profile_snapshot(tmp_path: Path) -> None:
    compiled = _compile(tmp_path, _row())
    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.dte_min == 3
    assert deployment.execution.dte_max == 7
    assert deployment.execution.dte_fallback_policy == "allow_nearest_after"
    assert deployment.risk.max_trade_premium_usd == 400.0
    assert deployment.risk.max_contracts == 1
    metadata = deployment.source.metadata
    assert metadata["requested_max_trade_premium_usd"] == 500.0
    assert metadata["operator_max_trade_premium_usd"] == 400.0
    assert metadata["effective_max_trade_premium_usd"] == 400.0


def test_operator_sheet_rows_resolve_global_and_profile_values() -> None:
    defaults = load_operator_defaults_sheet_rows(
        [
            {
                "section": "default",
                "key": "dte_fallback_policy",
                "value": "allow_nearest_after",
            },
            {
                "section": "default",
                "key": "max_trade_premium_usd",
                "value": 500,
            },
            {
                "section": "default",
                "key": "delta_min",
                "value": 0.15,
            },
            {
                "section": "default",
                "key": "delta_max",
                "value": 0.35,
            },
            {
                "section": "default",
                "key": "min_open_interest",
                "value": 100,
            },
            {
                "section": "default",
                "key": "max_bid_ask_spread_pct",
                "value": 0.2,
            },
            {
                "section": "profile__trend_continuation",
                "key": "dte_min",
                "value": 3,
            },
            {
                "section": "profile__trend_continuation",
                "key": "dte_max",
                "value": 7,
            },
            {
                "section": "profile__trend_continuation",
                "key": "max_trade_premium_usd",
                "value": 500,
            },
            {
                "section": "profile__trend_continuation",
                "key": "max_contracts",
                "value": 1,
            },
        ]
    )
    bundle = profile_bundle("TREND_CONTINUATION", defaults)
    assert bundle["execution"]["dte_fallback_policy"] == "allow_nearest_after"
    assert bundle["execution"]["dte_min"] == 3
    assert bundle["execution"]["dte_max"] == 7


def test_cartographer_accepts_authoritative_google_formatted_ceiling(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    defaults = _operator_defaults("400")
    compiled = compile_active_plan_from_rows(
        rows=[_row(defaults)],
        strategy_catalog_path=catalog,
        trading_date="2026-08-17",
        operator_defaults=defaults,
    )
    assert compiled.plan.suppressed == []
    assert compiled.plan.deployments[0].risk.max_trade_premium_usd == 400.0


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


def test_cartographer_sheet_profile_change_requires_matching_snapshot(
    tmp_path: Path,
) -> None:
    defaults = _operator_defaults()
    row = _row(defaults)
    changed = _operator_defaults()
    changed["profile__trend_continuation"]["dte_max"] = 9
    compiled = _compile(tmp_path, row, changed)
    assert "snapshot hash" in compiled.plan.suppressed[0]["reason"]


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
    assert fact["status"] == "closed"
    assert "decision_ready" not in fact


@pytest.mark.asyncio
async def test_terminal_owner_fact_emits_four_excursions_when_coverage_is_complete(
    tmp_path: Path,
) -> None:
    deployment = _compile(tmp_path, _row()).plan.deployments[0]
    events = _Events()
    supervisor = object.__new__(ExecutionSupervisor)
    supervisor.event_repository = events
    entry_at = datetime(2026, 8, 17, 15, tzinfo=UTC)
    exit_at = entry_at.replace(minute=2)
    supervisor._cartographer_excursion_observations = {
        "trade-1": {
            "option_marks": [
                {
                    "trade_id": "trade-1",
                    "timestamp": entry_at.replace(minute=1),
                    "price": 1.1,
                    "coverage": "complete",
                }
            ],
            "underlying_bars": [
                {
                    "start": entry_at,
                    "end": entry_at.replace(minute=1),
                    "high": 101.0,
                    "low": 99.0,
                    "coverage": "complete",
                },
                {
                    "start": entry_at.replace(minute=1),
                    "end": exit_at,
                    "high": 102.0,
                    "low": 98.0,
                    "coverage": "complete",
                },
            ],
        }
    }
    position = TrackedPosition(
        symbol="SPY",
        deployment_id=deployment.deployment_id,
        trade_id="trade-1",
        option_symbol="SPY_CALL",
        quantity=1,
        entry_price=1.0,
        underlying_entry_price=100.0,
        entry_timestamp=entry_at,
        source="shadow",
    )

    await supervisor._record_cartographer_terminal_fact(
        deployment,
        position,
        terminal_reason="profile_exit",
        fill_details={"exit_price": 1.25, "exit_filled_at": exit_at},
    )

    _, fact = events.rows[0]
    assert fact["option_mfe_pct"] == 0.25
    assert fact["option_mae_pct"] == 0.0
    assert fact["underlying_mfe_pct"] == 0.02
    assert fact["underlying_mae_pct"] == -0.02
    assert fact["coverage"]["option"]["status"] == "complete"
    assert fact["coverage"]["underlying"]["status"] == "complete"
