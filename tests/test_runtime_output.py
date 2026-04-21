"""Tests for runtime entry output formatting helpers."""

from types import SimpleNamespace

from bhiksha.app.runtime import (
    _cash_guard_reservation_summary,
    _entry_blocked_extra_details,
)


def _plan(**kwargs):
    return SimpleNamespace(**kwargs)


def test_entry_blocked_extra_details_surfaces_cash_guard_fields():
    plan = _plan(
        risk_details={
            "required_cash": 290.04,
            "buying_power_requirement": 290.04,
            "estimated_cost": 289.98,
            "remaining_budget": 142.5,
            "usable_budget": 142.5,
            "broker_cash_only_buying_power": 150.0,
            "buffer_pct": 0.05,
            "account_type": "CASH",
            "cash_guard_mode": "on",
        }
    )
    result = _entry_blocked_extra_details(plan)
    assert "account_type=CASH" in result
    assert "required_cash=290.04" in result
    assert "usable_budget=142.50" in result
    assert "remaining_budget=142.50" in result
    assert "broker_cash_only_buying_power=150.00" in result
    assert "buffer_pct=0.05" in result


def test_entry_blocked_extra_details_still_handles_insufficient_budget():
    plan = _plan(
        risk_details={
            "reason": "insufficient_budget",
            "max_premium": 300.0,
            "entry_price": 9.1,
            "min_contract_cost": 910.0,
        }
    )
    result = _entry_blocked_extra_details(plan)
    assert "max_premium=300.00" in result
    assert "entry_price=9.10" in result
    assert "min_contract_cost=910.00" in result
    # Should NOT contain cash guard fields
    assert "broker_cash_only_buying_power" not in result


def test_entry_blocked_extra_details_returns_empty_for_unknown():
    plan = _plan(risk_details={"reason": "some_other_reason"})
    assert _entry_blocked_extra_details(plan) == ""


def test_entry_blocked_extra_details_handles_missing_risk_details():
    plan = _plan()
    assert _entry_blocked_extra_details(plan) == ""


def test_cash_guard_reservation_summary_surfaces_reserved_fields():
    plan = _plan(
        risk_details={
            "required_cash": 400.0,
            "reserved_cash": 400.0,
            "remaining_budget": 550.0,
            "usable_budget": 950.0,
            "broker_cash_only_buying_power": 1000.0,
            "buffer_pct": 0.05,
            "account_type": "CASH",
            "cash_guard_mode": "on",
        }
    )
    result = _cash_guard_reservation_summary(plan)
    assert "reserved_cash=400.00" in result
    assert "remaining_budget=550.00" in result
    assert "usable_budget=950.00" in result
    # Should be concise — only the three reservation fields
    assert "broker_cash_only_buying_power" not in result
    assert "buffer_pct" not in result


def test_cash_guard_reservation_summary_returns_empty_without_reservation():
    plan = _plan(risk_details={"required_cash": 400.0})
    assert _cash_guard_reservation_summary(plan) == ""

    plan_no_details = _plan(risk_details={})
    assert _cash_guard_reservation_summary(plan_no_details) == ""
