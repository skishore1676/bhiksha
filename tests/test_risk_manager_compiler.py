from __future__ import annotations

import csv
import json
from pathlib import Path

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    compile_active_plan_from_rows,
    compile_active_plan_from_sheet,
)


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
# Gate-override audit: a row that sets profile-exit live-dispatch-gate inputs
# through execution/exit overrides is HONORED (the Sheet is the operator's
# sanctioned arming surface — it already controls mode=live) but every
# occurrence is surfaced in plan.summary for the audit trail.
# Policy changed 2026-07-02: strip -> surface (stripping would silently
# disarm the operator's pre-staged live flip on the next compile).
# --------------------------------------------------------------------------


def test_row_gate_override_keys_are_honored_and_surfaced(tmp_path: Path) -> None:
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
    # The dispatch-gate inputs set by the row are HONORED (operator surface).
    assert deployment.execution.runtime_mode == "live_automated"
    assert deployment.exit.profile_exit_drives_live is True
    # Legitimate override keys in the SAME payload still apply.
    assert deployment.execution.dte_min == 1
    assert deployment.exit.stop_loss_pct == 0.30

    # ...and every gate key is surfaced in the compiled-plan audit trail.
    warnings = compiled.plan.summary["gate_override_key_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["row_id"] == "hostile_row"
    assert set(warnings[0]["gate_keys"]) == {"execution_overrides.runtime_mode", "exit_overrides.profile_exit_drives_live"}
    # NOTE: runtime_mode "live_automated" still cannot dispatch a profile exit —
    # the runtime allowlist only opens for "live_approval_gated".


def test_shadow_only_override_key_is_also_surfaced() -> None:
    row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "r1",
            "row_type": "strategy",
            "strategy_id": "spy_strategy_v1",
            "authorization_mode": "shadow",
            "execution_overrides": {"shadow_only": False},
        }
    )
    assert row.execution_overrides == {"shadow_only": False}
    assert row.gate_override_keys == ["execution_overrides.shadow_only"]


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
    assert row.gate_override_keys == []


# --------------------------------------------------------------------------
# Sheet authorization authority
# --------------------------------------------------------------------------


def test_compile_active_plan_from_rows_preserves_sheet_live_authorization(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy.yaml", strategy_id="spy_strategy_v1", symbol="SPY")

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
    )

    assert compiled.plan.deployments[0].execution.shadow_only is False
    assert "risk_demotion_warnings" not in compiled.plan.summary
