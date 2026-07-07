from __future__ import annotations

from bhiksha.risk.plan_operator_defaults_source import PlanOperatorDefaultsSource
from bhiksha.risk.risk_settings import resolve_risk_settings


def test_resolve_risk_settings_uses_sheet_value_when_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", raising=False)
    source = PlanOperatorDefaultsSource({"max_daily_drawdown_pct": "1.5"})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.max_daily_drawdown_pct == 1.5
    assert settings.validation_warnings == ()


def test_resolve_risk_settings_env_wins_over_sheet(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "4.0")
    source = PlanOperatorDefaultsSource({"max_daily_drawdown_pct": "1.5"})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.max_daily_drawdown_pct == 4.0


def test_resolve_risk_settings_falls_back_to_default_when_sheet_and_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", raising=False)
    source = PlanOperatorDefaultsSource({})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.max_daily_drawdown_pct == 2.0  # module default


def test_resolve_risk_settings_invalid_sheet_value_warns_and_uses_default(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", raising=False)
    source = PlanOperatorDefaultsSource({"max_daily_drawdown_pct": "not-a-number"})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.max_daily_drawdown_pct == 2.0
    # The warning is keyed by the env-var-shaped name (resolve_risk_settings
    # doesn't know whether the raw value came from env or sheet), not the
    # sheet's lowercase key.
    assert any("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT" in warning for warning in settings.validation_warnings)


def test_resolve_risk_settings_sheet_covers_all_documented_keys(monkeypatch) -> None:
    for env_key in (
        "BHIKSHA_RISK_RAIL_A_ENABLED",
        "BHIKSHA_RISK_RAIL_B_ENABLED",
        "BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_DEMOTE_WINDOW",
        "BHIKSHA_RISK_DEMOTE_MIN_N",
        "BHIKSHA_RISK_DEMOTE_THRESHOLD_USD",
        "BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT",
    ):
        monkeypatch.delenv(env_key, raising=False)
    source = PlanOperatorDefaultsSource(
        {
            "rail_a_enabled": "false",
            "rail_b_enabled": "false",
            "max_daily_drawdown_pct": "1.0",
            "flatten_daily_drawdown_pct": "2.0",
            "demote_window": "5",
            "demote_min_n": "3",
            "demote_threshold_usd": "-50",
            "open_drawdown_warn_pct": "0.6",
        }
    )

    settings = resolve_risk_settings(settings_source=source)

    assert settings.rail_a_enabled is False
    assert settings.rail_b_enabled is False
    assert settings.max_daily_drawdown_pct == 1.0
    assert settings.flatten_daily_drawdown_pct == 2.0
    assert settings.demote_window == 5
    assert settings.demote_min_n == 3
    assert settings.demote_threshold_usd == -50.0
    assert settings.open_drawdown_warn_pct == 0.6
    assert settings.validation_warnings == ()


def test_plan_operator_defaults_source_from_active_plan_reads_flat_dict() -> None:
    active_plan = {
        "active_plan_id": "active_plan_2026-07-02",
        "operator_defaults": {"max_daily_drawdown_pct": "1.25", "option_stop_pct": "0.35"},
    }

    source = PlanOperatorDefaultsSource.from_active_plan(active_plan)

    assert source.get("max_daily_drawdown_pct") == "1.25"
    # Non-risk keys are present but simply unused by resolve_risk_settings.
    assert source.get("option_stop_pct") == "0.35"
    assert source.get("missing_key") is None


def test_plan_operator_defaults_source_from_active_plan_handles_none_and_missing() -> None:
    assert PlanOperatorDefaultsSource.from_active_plan(None).get("max_daily_drawdown_pct") is None
    assert PlanOperatorDefaultsSource.from_active_plan({}).get("max_daily_drawdown_pct") is None
    assert (
        PlanOperatorDefaultsSource.from_active_plan({"active_plan_id": "x"}).get("max_daily_drawdown_pct")
        is None
    )


# --------------------------------------------------------------------------
# Operator audit P4 (2026-07-06): open_drawdown_warn_pct sheet/env/default
# precedence, following the exact same env > sheet > default pattern as
# max_daily_drawdown_pct above -- the only difference is the "default" is
# None (unset), not a hardcoded number; RiskManager.effective_open_drawdown_
# warn_pct applies the "unset -> max_daily_drawdown_pct" fallback at the
# point of use (see test_risk_manager.py).
# --------------------------------------------------------------------------


def test_resolve_open_drawdown_warn_pct_uses_sheet_value_when_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", raising=False)
    source = PlanOperatorDefaultsSource({"open_drawdown_warn_pct": "0.9"})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.open_drawdown_warn_pct == 0.9
    assert settings.validation_warnings == ()


def test_resolve_open_drawdown_warn_pct_env_wins_over_sheet(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", "1.1")
    source = PlanOperatorDefaultsSource({"open_drawdown_warn_pct": "0.9"})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.open_drawdown_warn_pct == 1.1


def test_resolve_open_drawdown_warn_pct_falls_back_to_none_when_sheet_and_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", raising=False)
    source = PlanOperatorDefaultsSource({})

    settings = resolve_risk_settings(settings_source=source)

    assert settings.open_drawdown_warn_pct is None  # "unset" -- not a numeric default
