from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from bhiksha.active_plan.compiler import (
    compile_active_plan_from_google_sheets,
    compile_active_plan_from_sheet,
    sync_google_strategy_catalog,
)
from bhiksha.config.loader import load_active_plan
from bhiksha.tools.compile_active_plan import main as compile_active_plan_main


def test_compile_active_plan_from_csv_supports_strategy_and_manual_same_symbol(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_jerk.yaml", strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_jerk_live_today",
                "row_type": "strategy",
                "strategy_id": "spy_jerk_pivot_short_v1",
                "authorization_mode": "live",
                "max_trade_premium_usd": "200",
                "entry_window_start_et": "09:40",
                "notes": "primary opening lane",
                "execution_overrides": json.dumps({"dte_min": 1, "dte_max": 5}),
            },
            {
                "row_id": "spy_breakout_manual",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                "after_time_et": "09:35",
                "profit_target_multiple": "2.0",
                "stop_loss_pct": "0.35",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-09",
        trading_date="2026-04-09",
        source_name="test_sheet",
    )

    assert compiled.plan.active_plan_id == "active_plan_2026-04-09"
    assert compiled.plan.summary["deployment_count"] == 2
    assert compiled.plan.summary["symbols"] == ["SPY"]
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "spy_jerk_live_today",
        "spy_breakout_manual",
    ]

    strategy = compiled.plan.deployments[0]
    assert strategy.execution.shadow_only is False
    assert strategy.execution.dte_min == 1
    assert strategy.execution.dte_max == 5
    assert strategy.execution.entry_window_start_et == "09:40"
    assert strategy.risk.max_trade_premium_usd == 200
    assert strategy.source.origin == "active_sheet_strategy"
    assert strategy.source.metadata["strategy_id"] == "spy_jerk_pivot_short_v1"

    manual = compiled.plan.deployments[1]
    assert manual.strategy.key == "manual_trigger"
    assert manual.execution.shadow_only is True
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.exit.use_profit_target is True
    assert manual.exit.profit_target_multiple == 2.0
    assert manual.exit.stop_loss_pct == 0.35
    assert manual.source.metadata["manual_setup_type"] == "manual_trigger"


def test_compile_active_plan_suppresses_unknown_strategy_id(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    sheet_path = tmp_path / "sheet.json"
    sheet_path.write_text(
        json.dumps(
            [
                {
                    "row_id": "unknown_strategy_lane",
                    "row_type": "strategy",
                    "strategy_id": "missing_strategy",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-09",
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "Unknown strategy_id" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_cli_writes_active_plan_json(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "qqq_impulse.yaml", strategy_id="qqq_market_impulse_short_v1", symbol="QQQ")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "qqq_impulse_shadow",
                "row_type": "strategy",
                "strategy_id": "qqq_market_impulse_short_v1",
                "authorization_mode": "shadow",
            }
        ],
    )
    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"

    exit_code = compile_active_plan_main(
        [
            "--sheet",
            str(sheet_path),
            "--strategy-catalog",
            str(catalog_root),
            "--out",
            str(output_path),
            "--active-plan-id",
            "active_plan_2026-04-09",
            "--trading-date",
            "2026-04-09",
        ]
    )

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-09"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["qqq_impulse_shadow"]


def test_compile_active_plan_accepts_operator_friendly_alias_columns(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "live",
                "strategy": "market_impulse_spy_short_v1",
                "max_premium": "180",
                "start": "09:40",
                "end": "10:30",
            },
            {
                "id": "spy_breakout_lane",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "spy",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "09:35",
                "target_r": "2.5",
                "stop_pct": "0.30",
                "flat_time": "15:50",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-10",
        trading_date="2026-04-10",
    )

    strategy = compiled.plan.deployments[0]
    assert strategy.deployment_id == "spy_strategy_lane"
    assert strategy.execution.shadow_only is False
    assert strategy.execution.entry_window_start_et == "09:40"
    assert strategy.execution.entry_window_end_et == "10:30"
    assert strategy.risk.max_trade_premium_usd == 180

    manual = compiled.plan.deployments[1]
    assert manual.deployment_id == "spy_breakout_lane"
    assert manual.symbol == "SPY"
    assert manual.execution.shadow_only is True
    assert manual.strategy.key == "manual_breakout"
    assert manual.strategy.params["trigger_price"] == 603.25
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.execution.dte_max == 5
    assert manual.execution.target_abs_delta_min == 0.30
    assert manual.execution.target_abs_delta_max == 0.70
    assert manual.execution.min_open_interest == 50
    assert manual.execution.max_bid_ask_spread_pct == 0.25
    assert manual.exit.stop_loss_pct == 0.30
    assert manual.exit.profit_target_multiple == 2.5
    assert manual.exit.hard_flat_time_et == "15:50"
    assert manual.source.metadata["manual_setup_type"] == "breakout"


def test_compile_active_plan_normalizes_loose_sheet_times(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
                "start": "9:30",
                "end": "10:05",
            },
            {
                "id": "spy_breakout_lane",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "9:35",
                "flat_time": "15:05",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-10",
        trading_date="2026-04-10",
    )

    strategy = compiled.plan.deployments[0]
    manual = compiled.plan.deployments[1]
    assert strategy.execution.entry_window_start_et == "09:30"
    assert strategy.execution.entry_window_end_et == "10:05"
    assert manual.strategy.key == "manual_breakout"
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.exit.hard_flat_time_et == "15:05"


def test_compile_active_plan_suppresses_invalid_rows_but_keeps_valid_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
            },
            {
                "id": "bad_manual_lane",
                "type": "manual",
                "setup": "reversion",
                "mode": "shadow",
                "symbol": "TSLA",
                "direction": "short",
                "trigger": "250",
                "trigger_when": "BELOW",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-10",
    )

    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == ["spy_strategy_lane"]
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "bad_manual_lane"
    assert compiled.plan.suppressed[0]["sheet_name"] == "sheet.csv"
    assert "manual_setup_type=manual_trigger or breakout" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_manual_row_with_invalid_after_time(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
            },
            {
                "id": "bad_manual_after",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "bad-time",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-10",
    )

    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == ["spy_strategy_lane"]
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "bad_manual_after"
    assert "Invalid time value" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_from_google_sheets_uses_catalog_active_and_manual_tabs(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_v1",
                "playbook_id": "pb_spy_01",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "strategy_family": "impulse",
                "bhiksha_ready": "TRUE",
                "expectancy": "1.42",
                "confidence": "0.67",
                "thesis_exit_policy": "market_impulse_reclaim",
                "steward_recommendation": "shadow",
                "steward_notes": '{"rank":2,"reason":"advisory only"}',
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategies",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "live",
                "strategy": "market_impulse_spy_short_v1",
                "max_premium": "180",
                "start": "09:40",
            }
        ],
    )
    manual_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="manual_entry",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "shadow",
                "strategy": "breakout",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "09:35",
                "end_in_days": "1",
                "notes": "opening breakout",
                "id": "spy_breakout_lane",
            }
        ],
    )

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategies",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-11",
        trading_date="2026-04-11",
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.active_plan_id == "active_plan_2026-04-11"
    assert compiled.plan.source["spreadsheet_id"] == "spreadsheet123"
    assert compiled.plan.source["catalog_sheet_name"] == "strategy catalog"
    assert compiled.plan.source["strategy_sheet_name"] == "active_strategies"
    assert compiled.plan.source["manual_sheet_name"] == "manual_entry"
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "strategy_market_impulse_spy_short_v1_live_row_2",
        "spy_breakout_lane",
    ]
    strategy = compiled.plan.deployments[0]
    manual = compiled.plan.deployments[1]
    assert strategy.source.metadata["row_index"] == 2
    assert strategy.source.metadata["catalog_key"] == "market_impulse_spy_short_v1"
    assert strategy.source.metadata["playbook_id"] == "pb_spy_01"
    assert strategy.source.metadata["expectancy"] == 1.42
    assert "steward_recommendation" not in strategy.source.metadata
    assert "steward_notes" not in strategy.source.metadata
    assert manual.source.metadata["row_index"] == 2
    assert manual.strategy.key == "manual_breakout"
    assert manual.execution.dte_max == 1
    assert manual.exit.stop_loss_pct == 0.35
    assert manual.exit.profit_target_multiple == 1.25
    assert manual.exit.hard_flat_time_et == "15:53"
    assert manual.source.metadata["manual_setup_type"] == "breakout"
    assert manual.source.metadata["notes"] == "opening breakout"


def test_compile_active_plan_from_google_sheets_suppresses_non_ready_catalog_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_v1",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "bhiksha_ready": "FALSE",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategies",
        rows=[{"strategy": "market_impulse_spy_short_v1"}],
    )
    manual_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="manual_entry",
        rows=[],
    )

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategies",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not bhiksha_ready" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_from_google_sheets_promotes_google_catalog_entries(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_19383a3c9faf",
                "playbook_id": "market_impulse_spy_short_17d4462c5932",
                "symbol": "SPY",
                "bias_template": "bearish_trend_intraday",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "active",
                "bhiksha_ready": "TRUE",
                "last_validated_date": "2026-04-01",
                "thesis_exit_policy": "fixed_rr_underlying",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {
                            "direction": "short",
                            "entry_buffer_minutes": 3,
                            "entry_window_minutes": 45,
                            "regime_timeframe": "1h",
                        },
                        "vehicle_mapping": {
                            "profile": "single_leg_long_premium_v1",
                            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                            "dte_min": 0,
                            "dte_max": 7,
                            "target_abs_delta_min": 0.2,
                            "target_abs_delta_max": 0.4,
                        },
                        "catastrophe_exit_params": {
                            "hard_flat_time_et": "15:55",
                            "stop_loss_pct": 0.45,
                            "use_profit_target": False,
                        },
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0035,
                            "take_profit_underlying_r_multiple": 1.5,
                        },
                    }
                ),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[{"enabled": "TRUE", "mode": "live", "strategy": "market_impulse_spy_short_19383a3c9faf"}],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    generated_path = catalog_root / "google_promoted" / "market_impulse_spy_short_19383a3c9faf.yaml"
    assert generated_path.exists()
    generated_payload = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    assert generated_payload["strategy_id"] == "market_impulse_spy_short_19383a3c9faf"
    assert generated_payload["strategy"]["key"] == "market_impulse"
    assert generated_payload["exit"]["thesis_exit_policy"] == "fixed_rr_underlying"
    assert generated_payload["exit"]["use_algorithmic_exit"] is False
    assert compiled.plan.deployments[0].deployment_id == "strategy_market_impulse_spy_short_19383a3c9faf_live_row_2"
    assert compiled.plan.deployments[0].strategy.key == "market_impulse"
    assert compiled.plan.deployments[0].exit.use_algorithmic_exit is False


def test_compile_active_plan_maps_mala_v2_compact_playbook_summary(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "playbook_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "candidate",
                "bhiksha_ready": "TRUE",
                "operator_status_override": "shadow",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {
                            "entry_buffer_minutes": 5,
                            "entry_window_minutes": 90,
                            "regime_timeframe": "1h",
                            "vwma_periods": [5, 13, 21],
                        },
                        "bhiksha_compatibility": {
                            "supported": False,
                            "note": "mala_v2 candidate — pending bhiksha config review",
                        },
                        "vehicle_mapping": {
                            "structure": "long_put",
                            "dte": "7-21",
                            "delta_plan": "long 0.35-0.55 / short 0.10-0.25",
                            "entry_window_et": "09:45-14:30",
                            "risk_rule": "hard stop at -35% premium",
                        },
                        "catastrophe_exit_params": {
                            "hard_flat_time_et": "15:55",
                            "stop_loss_pct": 0.35,
                        },
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0075,
                            "take_profit_underlying_r_multiple": 2.0,
                        },
                    }
                ),
                "thesis_exit_policy": "fixed_rr_underlying",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "shadow",
                "strategy": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    deployment = compiled.plan.deployments[0]
    assert deployment.strategy.params["vwma_periods"] == [5, 13, 21]
    assert deployment.execution.dte_min == 7
    assert deployment.execution.dte_max == 21
    assert deployment.execution.target_abs_delta_min == 0.35
    assert deployment.execution.target_abs_delta_max == 0.55
    assert deployment.execution.entry_window_start_et == "09:45"
    assert deployment.execution.entry_window_end_et == "14:30"
    assert deployment.risk.stop_loss_pct == 0.35
    assert deployment.exit.thesis_exit_policy == "fixed_rr_underlying"
    assert deployment.exit.use_algorithmic_exit is False
    assert deployment.exit.thesis_exit_params == {
        "stop_loss_underlying_pct": 0.0075,
        "take_profit_underlying_r_multiple": 2.0,
    }
    compatibility = deployment.source.metadata["playbook_summary"]["bhiksha_compatibility"]
    assert compatibility["bhiksha_ready"] is True
    assert compatibility["supported"] is True
    assert compatibility["note"] == "bhiksha strategy and exit policy both implemented"


def test_compile_active_plan_can_use_mala_evidence_and_operator_defaults(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__iwm_long",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "IWM",
                "direction": "long",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_params_json": json.dumps(
                    {
                        "entry_buffer_minutes": 5,
                        "entry_window_minutes": 45,
                        "regime_timeframe": "15m",
                        "vwma_periods": [5, 13, 21],
                    }
                ),
                "signal_window_et": "09:35-10:15",
                "recommendation_tier": "shadow",
                "expectancy": "0.56",
                "confidence": "0.98",
                "signal_count": "49",
                "execution_robustness": "1.0",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
                "exit_reliability": "thin",
                "warnings": "legacy_m5_execution_mapping_ignored:entry_window_et",
            }
        ],
    )
    defaults_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Operator_Defaults_v1",
        rows=[
            {"section": "default", "key": "execution_window_start_et", "value": "09:30"},
            {"section": "default", "key": "execution_window_end_et", "value": "16:00"},
            {"section": "default", "key": "max_trade_premium_usd", "value": "500"},
            {"section": "default", "key": "option_stop_pct", "value": "0.35"},
            {"section": "default", "key": "option_profit_target_enabled", "value": "TRUE"},
            {"section": "default", "key": "option_profit_target_pct", "value": "0.35"},
            {"section": "default", "key": "min_open_interest", "value": "25"},
            {"section": "default", "key": "max_bid_ask_spread_pct", "value": "0.10"},
            {"section": "default", "key": "dte_min", "value": "5"},
            {"section": "default", "key": "dte_max", "value": "21"},
            {"section": "default", "key": "delta_min", "value": "0.15"},
            {"section": "default", "key": "delta_max", "value": "0.40"},
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "live",
                "strategy_id": "market-impulse-all-basket-discovery__iwm_long",
                "entry_window_start_et": "09:30",
                "max_trade_premium_usd": "1000",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        defaults_sheet_name="Operator_Defaults_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        defaults_client=defaults_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    deployment = compiled.plan.deployments[0]
    assert deployment.strategy.params["entry_buffer_minutes"] == 5
    assert deployment.strategy.params["direction"] == "long"
    assert deployment.execution.dte_min == 5
    assert deployment.execution.dte_max == 21
    assert deployment.execution.target_abs_delta_min == 0.15
    assert deployment.execution.target_abs_delta_max == 0.40
    assert deployment.execution.min_open_interest == 25
    assert deployment.execution.max_bid_ask_spread_pct == 0.10
    assert deployment.execution.entry_window_start_et == "09:30"
    assert deployment.execution.entry_window_end_et == "16:00"
    assert deployment.risk.max_trade_premium_usd == 1000
    assert deployment.risk.stop_loss_pct == 0.35
    assert deployment.exit.use_algorithmic_exit is False
    assert deployment.exit.use_profit_target is True
    assert deployment.exit.option_profit_target_pct == 0.35
    assert deployment.exit.profit_target_multiple is None
    assert deployment.exit.thesis_exit_params == {
        "stop_loss_underlying_pct": 0.005,
        "take_profit_underlying_r_multiple": 2.0,
    }
    assert deployment.source.metadata["mala_handoff_version"] == 1
    assert deployment.source.metadata["strategy_variant"] == "cross_reclaim"
    assert deployment.source.metadata["bhiksha_capability_status"] == "supported"
    assert deployment.source.metadata["signal_window_et"] == "09:35-10:15"


def test_mala_evidence_preserves_explicit_bhiksha_ready_when_provider_columns_are_advisory(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "bhiksha_capability_status": "supported",
                "bhiksha_capability_reason": "runtime_verified",
                "provider_validation_status": "provider_watch",
                "provider_feature_risk": "yellow",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.summary["suppressed_count"] == 0
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "strategy_market_impulse_all_basket_discovery_amd_short_shadow_row_2"
    ]
    assert compiled.plan.deployments[0].source.metadata["bhiksha_ready"] is True


def test_compile_active_plan_suppresses_mala_evidence_without_thesis_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "bhiksha_capability_status": "supported",
                "bhiksha_capability_reason": "runtime_verified",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "exit_contract_missing" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_unsupported_mala_strategy_variant(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_high_close.yaml",
        strategy_id="mi-desc-high-close-semiconductors-m1__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "mi-desc-high-close-semiconductors-m1__amd_short",
                "hypothesis_id": "mi-desc-high-close-semiconductors-m1",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "MI High Close Reclaim",
                "strategy_params_json": json.dumps(
                    {
                        "entry_mode": "close_location_reclaim",
                        "min_close_location": 0.7,
                        "entry_buffer_minutes": 3,
                        "entry_window_minutes": 60,
                    }
                ),
                "recommendation_tier": "shadow",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "mi-desc-high-close-semiconductors-m1__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "unsupported_strategy_variant: market_impulse.close_location_reclaim" in compiled.plan.suppressed[0]["reason"]


def test_google_catalog_exit_controls_can_explicitly_enable_native_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_native_exit",
                "playbook_id": "playbook_123",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "active",
                "bhiksha_ready": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {"direction": "short"},
                        "vehicle_mapping": {"profile": "single_leg_long_premium_v1"},
                        "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0035,
                            "take_profit_underlying_r_multiple": 1.5,
                        },
                        "exit_controls": {"use_algorithmic_exit": True},
                    }
                ),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[{"enabled": "TRUE", "mode": "live", "strategy": "market_impulse_spy_short_native_exit"}],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments[0].exit.use_algorithmic_exit is True


def test_sync_google_strategy_catalog_writes_active_or_candidate_bhiksha_ready_supported_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    (catalog_root / "manual.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": "manual_preserved",
                "enabled": True,
                "symbol": "SPY",
                "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
                "execution": {"profile": "single_leg_long_premium_v1"},
                "risk": {"profile": "conservative_day1"},
                "exit": {"profile": "strategy_exit_v1"},
                "source": {"origin": "manual"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    written = sync_google_strategy_catalog(
        strategy_catalog_path=catalog_root,
        google_strategy_catalog=[
            _catalog_sheet_row(
                catalog_key="eligible_market_impulse",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=True,
            ),
            _catalog_sheet_row(
                catalog_key="candidate_shadow_market_impulse",
                symbol="AMD",
                strategy_key="market_impulse",
                lifecycle_status="candidate",
                bhiksha_ready=True,
                operator_status_override="shadow",
            ),
            _catalog_sheet_row(
                catalog_key="not_ready",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=False,
            ),
            _catalog_sheet_row(
                catalog_key="retired",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="retired",
                bhiksha_ready=True,
            ),
            _catalog_sheet_row(
                catalog_key="unsupported",
                symbol="SPY",
                strategy_key="not_in_registry",
                lifecycle_status="active",
                bhiksha_ready=True,
            ),
        ],
    )

    assert [path.name for path in written] == [
        "eligible_market_impulse.yaml",
        "candidate_shadow_market_impulse.yaml",
    ]
    assert (catalog_root / "google_promoted" / "eligible_market_impulse.yaml").exists()
    assert (catalog_root / "google_promoted" / "candidate_shadow_market_impulse.yaml").exists()
    assert not (catalog_root / "google_promoted" / "not_ready.yaml").exists()
    assert not (catalog_root / "google_promoted" / "retired.yaml").exists()
    assert not (catalog_root / "google_promoted" / "unsupported.yaml").exists()


def test_sync_google_strategy_catalog_preserves_existing_file_when_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    generated_root = catalog_root / "google_promoted"
    generated_root.mkdir(parents=True)
    existing_path = generated_root / "eligible_market_impulse.yaml"
    existing_path.write_text("strategy_id: previous\n", encoding="utf-8")

    def _fail_write(path: Path, payload: str) -> None:
        del path, payload
        raise OSError("disk full")

    monkeypatch.setattr("bhiksha.active_plan.compiler._atomic_yaml_write", _fail_write)

    with pytest.raises(OSError, match="disk full"):
        sync_google_strategy_catalog(
            strategy_catalog_path=catalog_root,
            google_strategy_catalog=[
                _catalog_sheet_row(
                    catalog_key="eligible_market_impulse",
                    symbol="SPY",
                    strategy_key="market_impulse",
                    lifecycle_status="active",
                    bhiksha_ready=True,
                )
            ],
        )

    assert existing_path.read_text(encoding="utf-8") == "strategy_id: previous\n"


def test_google_catalog_payload_preserves_explicit_zero_execution_limits(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"

    sync_google_strategy_catalog(
        strategy_catalog_path=catalog_root,
        google_strategy_catalog=[
            _catalog_sheet_row(
                catalog_key="zero_dte_market_impulse",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=True,
                playbook_summary_json={
                    "entry_params": {"direction": "short"},
                    "vehicle_mapping": {
                        "profile": "single_leg_long_premium_v1",
                        "dte_min": 0,
                        "dte_max": 0,
                        "min_open_interest": 0,
                        "target_abs_delta_min": 0,
                        "target_abs_delta_max": 0,
                    },
                    "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
                },
            )
        ],
        operator_defaults={"dte_max": 7, "min_open_interest": 100},
    )

    payload = yaml.safe_load((catalog_root / "google_promoted" / "zero_dte_market_impulse.yaml").read_text(encoding="utf-8"))
    assert payload["execution"]["dte_min"] == 0
    assert payload["execution"]["dte_max"] == 0
    assert payload["execution"]["min_open_interest"] == 0
    assert payload["execution"]["target_abs_delta_min"] == 0
    assert payload["execution"]["target_abs_delta_max"] == 0


def test_compile_active_plan_cli_supports_google_sheets_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "qqq_impulse.yaml", strategy_id="qqq_market_impulse_short_v1", symbol="QQQ")
    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"

    def _fake_compile(**kwargs):
        return compile_active_plan_from_google_sheets(
            spreadsheet_id="spreadsheet123",
            credentials_path=tmp_path / "credentials.json",
            catalog_sheet_name="strategy catalog",
            strategy_sheet_name="active_strategies",
            manual_sheet_name="manual_entry",
            strategy_catalog_path=kwargs["strategy_catalog_path"],
            active_plan_id=kwargs["active_plan_id"],
            trading_date=kwargs["trading_date"],
            catalog_client=_FakeSheetClient(
                spreadsheet_id="spreadsheet123",
                sheet_name="strategy catalog",
                rows=[
                    {
                        "catalog_key": "qqq_market_impulse_short_v1",
                        "symbol": "QQQ",
                        "strategy_key": "market_impulse",
                        "bhiksha_ready": "TRUE",
                    }
                ],
            ),
            strategy_client=_FakeSheetClient(
                spreadsheet_id="spreadsheet123",
                sheet_name="active_strategies",
                rows=[{"strategy": "qqq_market_impulse_short_v1"}],
            ),
            manual_client=_FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[]),
        )

    monkeypatch.setattr("bhiksha.tools.compile_active_plan.compile_active_plan_from_google_sheets", _fake_compile)

    exit_code = compile_active_plan_main(
        [
            "--google-sheet-id",
            "spreadsheet123",
            "--credentials-path",
            str(tmp_path / "credentials.json"),
            "--catalog-sheet-name",
            "strategy catalog",
            "--strategy-catalog",
            str(catalog_root),
            "--out",
            str(output_path),
            "--active-plan-id",
            "active_plan_2026-04-11",
            "--trading-date",
            "2026-04-11",
        ]
    )

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-11"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["strategy_qqq_market_impulse_short_v1_shadow_row_2"]


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
            "key": "jerk_pivot_momentum" if "jerk" in strategy_id else "market_impulse",
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
            "profile": "strategy_exit_v1",
            "use_algorithmic_exit": "jerk" not in strategy_id,
            "use_profit_target": False,
            "profit_target_multiple": None,
            "stop_loss_pct": 0.45,
            "stop_to_breakeven_after_r_multiple": None,
            "hard_flat_time_et": "15:55",
        },
        "source": {"origin": "test_catalog", "run_date": "2026-04-08", "artifact": "research.md"},
        "approval_status": "approved",
        "tags": ["test"],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class _FakeSheetClient:
    def __init__(self, *, spreadsheet_id: str, sheet_name: str, rows: list[dict[str, str]]) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._rows = rows

    def read_rows(self, *, range_suffix: str = "A1:ZZ2000") -> list[dict[str, str]]:
        del range_suffix
        return [
            {
                **row,
                "row_index": index,
            }
            for index, row in enumerate(self._rows, start=2)
        ]


def _catalog_sheet_row(**overrides):
    payload = {
        "catalog_key": "market_impulse_spy_short_19383a3c9faf",
        "playbook_id": "playbook_123",
        "symbol": "SPY",
        "bias_template": "bearish_trend_intraday",
        "strategy_key": "market_impulse",
        "strategy_family": "market_impulse",
        "direction": "short",
        "lifecycle_status": "active",
        "bhiksha_ready": True,
        "playbook_summary_json": {
            "entry_params": {"direction": "short"},
            "vehicle_mapping": {"profile": "single_leg_long_premium_v1"},
            "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
        },
    }
    payload.update(overrides)
    from bhiksha.active_plan.compiler import StrategyCatalogSheetRow

    return StrategyCatalogSheetRow.model_validate(payload)
