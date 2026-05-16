from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import json

from bhiksha.config.loader import (
    load_active_plan,
    load_bias_inputs,
    load_deployments,
    load_runtime_deployments,
    load_strategy_catalog,
)


def test_load_deployments_from_config_directory() -> None:
    deployments = load_deployments(Path("config/deployments"))
    ids = {deployment.deployment_id for deployment in deployments}
    assert ids >= {
        "jerk_pivot_momentum_tsla_short_v1",
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }
    tsla = next(deployment for deployment in deployments if deployment.deployment_id == "jerk_pivot_momentum_tsla_short_v1")
    assert tsla.enabled is False
    assert tsla.execution.shadow_only is True
    assert tsla.execution.dte_min == 7
    assert tsla.execution.dte_max == 21
    assert tsla.execution.target_abs_delta_min == 0.35
    assert tsla.execution.target_abs_delta_max == 0.55
    assert tsla.execution.entry_window_start_et == "09:45"
    assert tsla.execution.entry_window_end_et == "14:30"
    assert tsla.source.metadata["holdout_mean_exp_r"] == 0.8397


def test_load_deployments_recurses_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    generated = root / "generated"
    generated.mkdir(parents=True)
    _write_manifest(root / "manual.yaml", "manual_qqq")
    _write_manifest(generated / "generated.yaml", "generated_qqq")

    deployments = load_deployments(root)
    assert {deployment.deployment_id for deployment in deployments} == {"generated_qqq", "manual_qqq"}

    _write_manifest(generated / "duplicate.yaml", "manual_qqq")
    with pytest.raises(ValueError, match="Duplicate deployment_id"):
        load_deployments(root)


def test_load_runtime_deployments_prefer_generated_skips_manual_symbol_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    generated = root / "generated"
    generated.mkdir(parents=True)
    _write_manifest(root / "manual_spy.yaml", "manual_spy", symbol="SPY")
    _write_manifest(root / "manual_qqq.yaml", "manual_qqq", symbol="QQQ")
    _write_manifest(generated / "generated_spy.yaml", "generated_spy", symbol="SPY")

    deployments, report = load_runtime_deployments(
        root,
        generated_path=generated,
        selection_mode="prefer_generated",
    )

    assert {deployment.deployment_id for deployment in deployments} == {"generated_spy", "manual_qqq"}
    skipped_ids = {entry["deployment_id"] for entry in report["skipped"]}
    assert skipped_ids == {"manual_spy"}
    assert report["mode"] == "prefer_generated"


def test_load_runtime_deployments_generated_only_filters_manual_entries(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    generated = root / "generated"
    generated.mkdir(parents=True)
    _write_manifest(root / "manual_qqq.yaml", "manual_qqq", symbol="QQQ")
    _write_manifest(generated / "generated_spy.yaml", "generated_spy", symbol="SPY")

    deployments, report = load_runtime_deployments(
        root,
        generated_path=generated,
        selection_mode="generated_only",
    )

    assert [deployment.deployment_id for deployment in deployments] == ["generated_spy"]
    assert report["skipped"][0]["deployment_id"] == "manual_qqq"


def test_load_bias_inputs_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    assert load_bias_inputs(tmp_path / "missing.yaml") == []


def test_load_bias_inputs_accepts_reserved_emergency_controls(tmp_path: Path) -> None:
    path = tmp_path / "bias_inputs.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "emergency": {"halt_and_flatten": True},
                "selections": [
                    {
                        "symbol": "iwm",
                        "bias_template": "bullish_trend_intraday",
                        "horizon": "intraday",
                        "enabled": True,
                        "max_active_candidates": 1,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    selections = load_bias_inputs(path)
    assert len(selections) == 1
    assert selections[0].symbol == "IWM"


def test_load_active_plan_allows_duplicate_symbols_but_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload_path = tmp_path / "active_plan.json"
    payload_path.write_text(
        json.dumps(
            {
                "contract_name": "active_plan",
                "schema_version": 1,
                "active_plan_id": "active_plan_2026-04-01",
                "deployments": [
                    _manifest_dict("manual_spy", symbol="SPY"),
                    _manifest_dict("playbook_spy", symbol="SPY"),
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    plan = load_active_plan(payload_path)
    assert [deployment.deployment_id for deployment in plan.deployments] == ["manual_spy", "playbook_spy"]

    payload_path.write_text(
        json.dumps(
            {
                "contract_name": "active_plan",
                "schema_version": 1,
                "active_plan_id": "active_plan_2026-04-01",
                "deployments": [
                    _manifest_dict("manual_spy", symbol="SPY"),
                    _manifest_dict("manual_spy", symbol="QQQ"),
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate deployment_id"):
        load_active_plan(payload_path)


def test_load_active_plan_rejects_legacy_contracts(tmp_path: Path) -> None:
    payload_path = tmp_path / "active_session.json"
    payload_path.write_text(
        json.dumps(
            {
                "contract_name": "active_session",
                "schema_version": 1,
                "session_id": "active_session_2026-04-01",
                "session_date": "2026-04-01",
                "deployments": [_manifest_dict("legacy_spy", symbol="SPY")],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_active_plan(payload_path)


def test_load_strategy_catalog_from_config_directory() -> None:
    catalog = load_strategy_catalog(Path("config/strategy_catalog"))

    ids = {entry.strategy_id for entry in catalog}
    assert ids >= {
        "jerk_pivot_momentum_tsla_short_v1",
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }
    tsla = next(entry for entry in catalog if entry.strategy_id == "jerk_pivot_momentum_tsla_short_v1")
    assert tsla.enabled is False
    assert tsla.approval_status == "retired"
    assert tsla.execution.target_abs_delta_min == 0.35
    assert "mala_promoted" in tsla.tags


def test_load_deployments_normalizes_time_fields(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"]["entry_window_start_et"] = "9:45"
    payload["execution"]["entry_window_end_et"] = "9:55"
    payload["risk"]["hard_flat_time_et"] = "9:05"
    payload["exit"]["hard_flat_time_et"] = "9:07"
    payload["exit"]["catastrophe_exit_params"] = {"hard_flat_time_et": "9:08"}
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    deployments = load_deployments(root)

    assert deployments[0].execution.entry_window_start_et == "09:45"
    assert deployments[0].execution.entry_window_end_et == "09:55"
    assert deployments[0].risk.hard_flat_time_et == "09:05"
    assert deployments[0].exit.hard_flat_time_et == "09:07"
    assert deployments[0].exit.catastrophe_exit_params["hard_flat_time_et"] == "09:08"


def test_load_deployments_rejects_algorithmic_exit_without_fallback(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_tsla", symbol="TSLA")
    payload["strategy"]["key"] = "jerk_pivot_momentum"
    payload["exit"]["stop_loss_pct"] = 0.0
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="use_algorithmic_exit"):
        load_deployments(root)


def test_load_deployments_accepts_non_native_exit_with_thesis_fallback(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_tsla", symbol="TSLA")
    payload["strategy"]["key"] = "jerk_pivot_momentum"
    payload["exit"]["thesis_exit_policy"] = "fixed_rr_underlying"
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    deployments = load_deployments(root)

    assert deployments[0].exit.thesis_exit_policy == "fixed_rr_underlying"


def test_load_deployments_accepts_non_native_exit_with_profit_target_fallback(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_tsla", symbol="TSLA")
    payload["strategy"]["key"] = "jerk_pivot_momentum"
    payload["exit"]["use_profit_target"] = True
    payload["exit"]["profit_target_multiple"] = 1.5
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    deployments = load_deployments(root)

    assert deployments[0].exit.profit_target_multiple == 1.5


def test_load_strategy_catalog_rejects_algorithmic_exit_without_fallback(tmp_path: Path) -> None:
    root = tmp_path / "strategy_catalog"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_tsla", symbol="TSLA")
    payload["strategy"]["key"] = "jerk_pivot_momentum"
    payload["exit"]["stop_loss_pct"] = 0.0
    payload["strategy_id"] = "manual_tsla"
    payload["approval_status"] = "approved"
    payload["tags"] = []
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="use_algorithmic_exit"):
        load_strategy_catalog(root)


def test_load_deployments_rejects_invalid_entry_window_time(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"]["entry_window_start_et"] = "bad-time"
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="Invalid time value"):
        load_deployments(root)


def test_load_deployments_rejects_invalid_hard_flat_time(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["exit"]["hard_flat_time_et"] = "25:99"
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="Invalid time value"):
        load_deployments(root)


def _write_manifest(path: Path, deployment_id: str, *, symbol: str = "QQQ") -> None:
    payload = _manifest_dict(deployment_id, symbol=symbol)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _manifest_dict(deployment_id: str, *, symbol: str = "QQQ") -> dict:
    return {
        "deployment_id": deployment_id,
        "enabled": True,
        "symbol": symbol,
        "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
        "execution": {
            "profile": "single_leg_long_premium_v1",
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
            "profile": "market_impulse_exit_v1",
            "use_algorithmic_exit": True,
            "use_profit_target": False,
            "profit_target_multiple": None,
            "stop_loss_pct": 0.45,
            "stop_to_breakeven_after_r_multiple": None,
            "hard_flat_time_et": "15:55",
        },
        "source": {"origin": "test", "run_date": "2026-03-31", "artifact": "test.csv"},
    }
