from __future__ import annotations

import csv
import json
from pathlib import Path

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    apply_risk_demotion_overrides,
    compile_active_plan_from_rows,
    compile_active_plan_from_sheet,
)
from bhiksha.config.models import DeploymentManifest
from bhiksha.risk.demotion_store import DemotionStore


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_catalog_entry(path: Path, *, strategy_id: str, symbol: str) -> None:
    payload = {
        "strategy_id": strategy_id,
        "enabled": True,
        "symbol": symbol,
        "strategy": {
            "key": "market_impulse",
            "version": 1,
            "params": {"direction": "short"},
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": True,
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": 7,
            "target_abs_delta_min": 0.2,
            "target_abs_delta_max": 0.4,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.2,
        },
        "risk": {
            "profile": "conservative_day1",
            "max_trade_premium_usd": 300,
            "hard_flat_time_et": "15:55",
            "stop_loss_pct": 0.45,
        },
        "exit": {
            "profile": "strategy_managed_v1",
            "use_algorithmic_exit": True,
            "stop_loss_pct": 0.45,
            "hard_flat_time_et": "15:55",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# Deny-list regression: a hostile row cannot pre-stage the profile-exit
# live-dispatch-gate inputs through execution_overrides/exit_overrides.
# --------------------------------------------------------------------------


def test_hostile_row_cannot_prestage_dispatch_gate_inputs_via_overrides(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy.yaml", strategy_id="spy_strategy_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "hostile_row",
                "row_type": "strategy",
                "strategy_id": "spy_strategy_v1",
                "authorization_mode": "live",
                "execution_overrides": json.dumps(
                    {
                        "runtime_mode": "live_automated",
                        "dte_min": 1,  # legitimate key must survive
                    }
                ),
                "exit_overrides": json.dumps(
                    {
                        "profile_exit_drives_live": True,
                        "stop_loss_pct": 0.30,  # legitimate key must survive
                    }
                ),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="ap_test",
        trading_date="2026-04-20",
    )

    deployment = compiled.plan.deployments[0]
    # The two dispatch-gate inputs must NOT have been set by the hostile row.
    assert deployment.execution.runtime_mode is None
    assert deployment.exit.profile_exit_drives_live is False
    # Legitimate override keys in the SAME payload must still apply.
    assert deployment.execution.dte_min == 1
    assert deployment.exit.stop_loss_pct == 0.30

    warnings = compiled.plan.summary["denied_override_key_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["row_id"] == "hostile_row"
    assert set(warnings[0]["denied_keys"]) == {"execution_overrides.runtime_mode", "exit_overrides.profile_exit_drives_live"}


def test_shadow_only_override_key_is_also_denied() -> None:
    row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "r1",
            "row_type": "strategy",
            "strategy_id": "spy_strategy_v1",
            "authorization_mode": "shadow",
            "execution_overrides": {"shadow_only": False},
        }
    )
    assert "shadow_only" not in row.execution_overrides
    assert row.denied_override_keys == ["execution_overrides.shadow_only"]


def test_legitimate_override_keys_pass_through_untouched() -> None:
    row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "r1",
            "row_type": "strategy",
            "strategy_id": "spy_strategy_v1",
            "authorization_mode": "shadow",
            "execution_overrides": {"dte_min": 2, "dte_max": 6},
            "exit_overrides": {"stop_loss_pct": 0.2, "eod_flat": False},
        }
    )
    assert row.execution_overrides == {"dte_min": 2, "dte_max": 6}
    assert row.exit_overrides == {"stop_loss_pct": 0.2, "eod_flat": False}
    assert row.denied_override_keys == []


# --------------------------------------------------------------------------
# Rail B compiler hook: demotion override merge
# --------------------------------------------------------------------------


def _live_deployment(deployment_id: str) -> DeploymentManifest:
    return DeploymentManifest.model_validate(
        {
            "deployment_id": deployment_id,
            "enabled": True,
            "symbol": "SPY",
            "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
            "execution": {
                "profile": "single_leg_long_premium_v1",
                "shadow_only": False,
                "dte_min": 0,
                "dte_max": 7,
            },
            "risk": {"profile": "conservative_day1", "stop_loss_pct": 0.45},
        }
    )


def test_compiler_merges_demotion_override_forcing_row_shadow(tmp_path: Path) -> None:
    store = DemotionStore(tmp_path / "demoted_deployments.json")
    store.record_demotion(
        deployment_id="dep_live_1",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-42.0,
        threshold_usd=0.0,
        trade_ids=[f"T{i}" for i in range(10)],
    )

    deployments = [_live_deployment("dep_live_1"), _live_deployment("dep_live_2")]
    updated, warnings = apply_risk_demotion_overrides(deployments, demotion_store=store)

    by_id = {deployment.deployment_id: deployment for deployment in updated}
    assert by_id["dep_live_1"].execution.shadow_only is True
    assert by_id["dep_live_2"].execution.shadow_only is False  # untouched
    assert len(warnings) == 1
    assert warnings[0]["deployment_id"] == "dep_live_1"


def test_compiler_ignores_unknown_demoted_deployment_id_with_warning(tmp_path: Path) -> None:
    store = DemotionStore(tmp_path / "demoted_deployments.json")
    store.record_demotion(
        deployment_id="dep_does_not_exist",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-10.0,
        threshold_usd=0.0,
        trade_ids=[],
    )

    deployments = [_live_deployment("dep_live_1")]
    updated, warnings = apply_risk_demotion_overrides(deployments, demotion_store=store)

    # The compile must not break, and the known deployment is untouched.
    assert len(updated) == 1
    assert updated[0].execution.shadow_only is False
    assert len(warnings) == 1
    assert warnings[0]["deployment_id"] == "dep_does_not_exist"
    assert warnings[0]["reason"] == "unknown_deployment_id"


def test_compiler_demotion_override_is_noop_when_no_demotions(tmp_path: Path) -> None:
    store = DemotionStore(tmp_path / "demoted_deployments.json")
    deployments = [_live_deployment("dep_live_1")]

    updated, warnings = apply_risk_demotion_overrides(deployments, demotion_store=store)

    assert updated == deployments
    assert warnings == []


def test_compile_active_plan_from_rows_applies_demotion_override_end_to_end(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy.yaml", strategy_id="spy_strategy_v1", symbol="SPY")

    store = DemotionStore(tmp_path / "demoted_deployments.json")
    store.record_demotion(
        deployment_id="live_row",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-15.0,
        threshold_usd=0.0,
        trade_ids=[],
    )

    row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "live_row",
            "row_type": "strategy",
            "strategy_id": "spy_strategy_v1",
            "authorization_mode": "live",
        }
    )

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="ap_test",
        trading_date="2026-04-20",
        risk_demotion_store=store,
    )

    assert compiled.plan.deployments[0].execution.shadow_only is True
    assert compiled.plan.summary["risk_demotion_warnings"][0]["deployment_id"] == "live_row"
