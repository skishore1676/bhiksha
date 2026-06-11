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


def _failure_runtime():
    from bhiksha.app.bootstrap import build_runtime

    return build_runtime()


class _RecordingRepository:
    def __init__(self):
        self.events = []

    async def append(self, event_type, payload):
        self.events.append((event_type, payload))


def _supervisor_with_repository():
    repository = _RecordingRepository()
    return SimpleNamespace(event_repository=repository), repository


def test_instrument_execution_runner_logs_selector_empty_without_nameerror():
    """Regression: the except path used to reference an undefined `output` name."""
    import asyncio

    from bhiksha.options.selectors import SelectorEmptyError

    runtime = _failure_runtime()
    supervisor, repository = _supervisor_with_repository()
    lines = []

    async def inner():
        raise SelectorEmptyError("amd_lane", {"total_candidates": 5, "open_interest_below_min": 5})

    async def run_once():
        runner = runtime._instrument_execution_runner(
            supervisor,
            "AMD",
            deployment_id="amd_lane",
            reconcile_trigger=asyncio.Event(),
            action="entry",
            inner=inner,
            output=lines.append,
        )
        await runner()

    asyncio.run(run_once())

    issues = [payload for event_type, payload in repository.events if event_type == "runtime_issue"]
    assert len(issues) == 1
    assert issues[0]["category"] == "entry_selector_empty"
    assert issues[0]["selector_breakdown"] == {"total_candidates": 5, "open_interest_below_min": 5}
    assert any(line.startswith("RUNTIME_ISSUE AMD") for line in lines)


def test_instrument_execution_runner_throttles_repeated_selector_empty_events():
    import asyncio

    from bhiksha.options.selectors import SelectorEmptyError

    runtime = _failure_runtime()
    supervisor, repository = _supervisor_with_repository()

    async def inner():
        raise SelectorEmptyError("amd_lane", {"total_candidates": 5})

    async def run_twice():
        for _ in range(2):
            runner = runtime._instrument_execution_runner(
                supervisor,
                "AMD",
                deployment_id="amd_lane",
                reconcile_trigger=asyncio.Event(),
                action="entry",
                inner=inner,
                output=lambda line: None,
            )
            await runner()

    asyncio.run(run_twice())

    issues = [payload for event_type, payload in repository.events if event_type == "runtime_issue"]
    assert len(issues) == 1


def test_dead_lane_alert_fires_once_for_live_lane():
    import asyncio

    runtime = _failure_runtime()
    supervisor, repository = _supervisor_with_repository()
    deployment = SimpleNamespace(
        deployment_id="amd_lane",
        symbol="AMD",
        execution=SimpleNamespace(shadow_only=False),
    )
    lines = []

    async def run_failures(count):
        for _ in range(count):
            await runtime._record_live_entry_failure(supervisor, deployment, live=True, output=lines.append)

    asyncio.run(run_failures(4))

    dead_lane_events = [
        payload for event_type, payload in repository.events
        if event_type == "runtime_issue" and payload["category"] == "dead_lane"
    ]
    assert len(dead_lane_events) == 1
    assert dead_lane_events[0]["deployment_id"] == "amd_lane"
    assert sum(1 for line in lines if line.startswith("DEAD_LANE")) == 1


def test_dead_lane_alert_skips_shadow_and_successful_lanes():
    import asyncio

    runtime = _failure_runtime()
    supervisor, repository = _supervisor_with_repository()
    shadow = SimpleNamespace(
        deployment_id="shadow_lane",
        symbol="MU",
        execution=SimpleNamespace(shadow_only=True),
    )
    succeeded = SimpleNamespace(
        deployment_id="good_lane",
        symbol="NVDA",
        execution=SimpleNamespace(shadow_only=False),
    )
    runtime._live_entry_success_ids.add("good_lane")

    async def run_failures():
        for _ in range(5):
            await runtime._record_live_entry_failure(supervisor, shadow, live=True, output=lambda line: None)
            await runtime._record_live_entry_failure(supervisor, succeeded, live=True, output=lambda line: None)

    asyncio.run(run_failures())

    assert repository.events == []
