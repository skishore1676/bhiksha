from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import json

from bhiksha.config.loader import (
    load_active_plan,
    load_app_config,
    load_bias_inputs,
    load_deployments,
    load_runtime_deployments,
    load_strategy_catalog,
)
from bhiksha.config.models import ActivePlan
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.risk_envelope_authority import (
    risk_envelope_authorization_fingerprint,
)
from historical_config import HISTORICAL_DEPLOYMENTS_DIR, HISTORICAL_STRATEGY_CATALOG_DIR


def test_load_deployments_from_config_directory() -> None:
    deployments = load_deployments(HISTORICAL_DEPLOYMENTS_DIR)
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


def test_load_app_config_allows_entry_reprice_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump({"app_name": "bhiksha"}), encoding="utf-8")
    monkeypatch.setenv("BHIKSHA_ENTRY_REPRICE_ENABLED", "true")
    monkeypatch.setenv("BHIKSHA_ENTRY_REPRICE_CHECKPOINTS_SECONDS", "15,45,120")
    monkeypatch.setenv("BHIKSHA_ENTRY_REPRICE_CANCEL_AFTER_SECONDS", "240")
    monkeypatch.setenv("BHIKSHA_ENTRY_REPRICE_SPREAD_PCTS", "0.4,0.75,1.0")

    config = load_app_config(path)

    assert config.entry_reprice_enabled is True
    assert config.entry_reprice_checkpoints_seconds == [15, 45, 120]
    assert config.entry_reprice_cancel_after_seconds == 240
    assert config.entry_reprice_spread_pcts == [0.4, 0.75, 1.0]


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
    catalog = load_strategy_catalog(HISTORICAL_STRATEGY_CATALOG_DIR)

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


def test_load_deployments_rejects_patient_entry_ladder_after_cancel_deadline(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"].update(
        {
            "entry_reprice_enabled": True,
            "entry_reprice_checkpoints_seconds": [60, 300],
            "entry_reprice_cancel_after_seconds": 300,
            "entry_reprice_spread_fractions": [0.50, 0.70],
        }
    )
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="before the cancel deadline"):
        load_deployments(root)


def test_load_deployments_rejects_mismatched_patient_entry_ladder(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"].update(
        {
            "entry_reprice_checkpoints_seconds": [60, 180],
            "entry_reprice_cancel_after_seconds": 300,
            "entry_reprice_spread_fractions": [0.50],
        }
    )
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="equal lengths"):
        load_deployments(root)


def test_load_deployments_rejects_partial_named_profile_ladder_override(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"].update(
        {
            "entry_execution_profile": "balanced",
            "entry_reprice_checkpoints_seconds": [30, 90],
        }
    )
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="must include matching spread fractions"):
        load_deployments(root)


def test_load_deployments_rejects_named_profile_checkpoint_at_profile_deadline(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"].update(
        {
            "entry_execution_profile": "balanced",
            "entry_reprice_checkpoints_seconds": [30, 150],
            "entry_reprice_spread_fractions": [0.60, 0.85],
        }
    )
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="before the cancel deadline"):
        load_deployments(root)


def test_load_deployments_rejects_reprice_chase_cap_above_one(tmp_path: Path) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict("manual_qqq")
    payload["execution"]["entry_reprice_max_chase_pct"] = 1.01
    (root / "manual.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValidationError, match="less than or equal to 1"):
        load_deployments(root)


def test_risk_envelope_canary_is_default_off_and_rejects_wrong_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    baseline = _manifest_dict("manual_qqq")
    (root / "baseline.yaml").write_text(
        yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8"
    )
    loaded = load_deployments(root)[0]
    assert loaded.exit.risk_envelope_live_mode == "off"
    assert loaded.exit.risk_envelope_live_candidate_id is None

    wrong = _manifest_dict("wrong_canary")
    wrong["exit"].update(
        {
            "profile_exit_drives_live": True,
            "risk_envelope_live_mode": "canary",
            "risk_envelope_live_candidate_id": "variant_a",
            "risk_envelope_live_candidate_overlay_hash": (
                "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e"
            ),
            "risk_envelope_live_authorization_id": "test-auth",
            "risk_envelope_live_max_premium_cap_fraction": 0.20,
        }
    )
    wrong["execution"].update(
        {
            "runtime_mode": "live_approval_gated",
            "dte_min": 4,
            "dte_max": 7,
        }
    )
    wrong["risk"]["max_contracts"] = 1
    (root / "wrong.yaml").write_text(
        yaml.safe_dump(wrong, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="safety_stack"):
        load_deployments(root)


def test_risk_envelope_canary_requires_one_contract_strict_4_to_7_dte(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployments"
    root.mkdir(parents=True)
    payload = _manifest_dict(
        "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3",
        symbol="IWM",
    )
    payload["exit"].update(
        {
            "profile_exit_id": "profile__trend_continuation",
            "target_1_r": 1.0,
            "target_2_r": 2.0,
            "target_1_quantity": 0.60,
            "no_progress_seconds": 2_700,
            "max_hold_seconds": 10_800,
            "breakeven_after_t1": True,
            "eod_flat": True,
            "profile_exit_drives_live": True,
            "risk_envelope_live_mode": "canary",
            "risk_envelope_live_candidate_id": "safety_stack",
            "risk_envelope_live_candidate_overlay_hash": (
                "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e"
            ),
            "risk_envelope_live_authorization_id": "test-auth",
            "risk_envelope_live_start_at": "2026-07-20T00:00:00+00:00",
            "risk_envelope_live_expires_at": "2026-08-01T00:00:00+00:00",
            "risk_envelope_live_authorized_deployment_id": (
                "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
            ),
            "risk_envelope_live_authorized_symbol": "IWM",
            "risk_envelope_live_authorized_active_plan_id": "active-plan-test",
            "risk_envelope_live_rollback_action": (
                "disable_canary_restore_control"
            ),
            "risk_envelope_live_max_premium_cap_fraction": 0.20,
        }
    )
    payload["execution"].update(
        {
            "runtime_mode": "live_approval_gated",
            "dte_min": 4,
            "dte_max": 7,
            "dte_fallback_policy": "strict",
        }
    )
    payload["risk"]["max_contracts"] = 1
    payload["risk"]["max_trade_premium_usd"] = 2_000.0
    frozen_policy = {
        "policy_schema_version": "exit-policy.v1",
        "policy_id": "profile__trend_continuation",
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.60,
        "no_progress_seconds": 2_700,
        "max_hold_seconds": 10_800,
        "breakeven_after_t1": True,
        "eod_flat": True,
    }
    payload["exit"].update(
        {
            "exit_policy_schema_version": "exit-policy.v1",
            "exit_policy_id": "profile__trend_continuation",
            "exit_policy_snapshot": frozen_policy,
            "exit_policy_hash": canonical_policy_hash(frozen_policy),
        }
    )
    path = root / "canary.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    canary = load_deployments(root)[0]
    assert canary.risk.max_contracts == 1
    plan = ActivePlan(
        active_plan_id="active-plan-test",
        deployments=[canary],
    )
    assert plan.risk_envelope_authorization_fingerprint == (
        risk_envelope_authorization_fingerprint(
            active_plan_id=plan.active_plan_id,
            deployments=plan.deployments,
        )
    )
    pdd_entry_canary = canary.model_copy(
        update={
            "deployment_id": "pdd_live_canary",
            "symbol": "PDD",
            "execution": canary.execution.model_copy(
                update={"shadow_only": False, "dte_min": 0, "dte_max": 3}
            ),
            "risk": canary.risk.model_copy(
                update={"max_trade_premium_usd": 300.0}
            ),
            "exit": canary.exit.model_copy(
                update={"risk_envelope_live_mode": "off"}
            ),
            "source": canary.source.model_copy(
                update={
                    "metadata": {
                        "strategy_id": "triage-market_impulse-PDD__pdd_long",
                        "authorization_mode": "live",
                    }
                }
            ),
        }
    )
    with pytest.raises(
        ValidationError,
        match="at most one experimental live authority",
    ):
        ActivePlan(
            active_plan_id="active-plan-test",
            deployments=[canary, pdd_entry_canary],
        )

    payload["risk"]["max_contracts"] = 2
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError, match="max_contracts=1"):
        load_deployments(root)

    payload["risk"]["max_contracts"] = 1
    payload["execution"]["dte_min"] = 5
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError, match="dte_min=4,dte_max=7"):
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
