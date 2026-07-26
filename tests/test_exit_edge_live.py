from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import inspect
import sqlite3
from threading import Event, Thread
import time

from bhiksha.config.loader import load_app_config
from bhiksha.execution.order_manager import OrderManager
from bhiksha.execution.supervisor import _confirmed_entry_fill_facts
from bhiksha.ops import exit_edge_live
from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    SHADOW_CANDIDATE_IDS,
)
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder
from bhiksha.execution.exit_policy import canonical_policy_hash


ENTRY = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
OPTION = "QQQ260710P00713000"


def _deployment():
    control_policy = {
        "policy_id": "exit.premium_envelope.trend_continuation.control.v1",
        "stop_family": "premium_pct",
        "stop_anchor": "filled_option_premium",
        "exit_family": "profile_ladder",
        "target_model": "staged_r",
        "target_r": 2.0,
        "hard_flat_time_et": "15:55",
        "option_stop_fallback_pct": 0.45,
        "target_order_mode": "virtual_or_broker",
        "source_config_id": None,
        "parameters": {},
        "policy_schema_version": "exit-policy.v1",
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.6,
        "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.45,
        "no_progress_seconds": 2700,
        "max_hold_seconds": 10800,
        "high_water_giveback_policy": "OFF",
        "giveback_arm_r": None,
        "giveback_retrace_fraction": None,
        "risk_envelope_enabled": False,
        "risk_envelope_activation_r": None,
        "risk_envelope_initial_floor_r": None,
        "risk_envelope_curvature": None,
        "risk_envelope_floor_at_t1_r": None,
        "risk_envelope_ratchet_step_r": None,
        "breakeven_after_t1": True,
        "eod_flat": True,
    }
    return SimpleNamespace(
        deployment_id="qqq-live",
        symbol="QQQ",
        exit=SimpleNamespace(
            profile_exit_id="profile__trend_continuation",
            profile="strategy_managed_v1",
            target_1_r=1.0,
            target_2_r=2.0,
            target_1_quantity=0.6,
            initial_stop_pct=0.35,
            premium_disaster_stop_pct=0.45,
            stop_loss_pct=0.35,
            profit_target_multiple=1.0,
            option_profit_target_pct=None,
            use_profit_target=True,
            no_progress_seconds=2700,
            max_hold_seconds=10800,
            high_water_giveback_policy="OFF",
            breakeven_after_t1=True,
            eod_flat=True,
            hard_flat_time_et="15:55",
            no_progress_favorable_floor_r=0.25,
            exit_policy_schema_version="exit-policy.v1",
            exit_policy_id=control_policy["policy_id"],
            exit_policy_hash=canonical_policy_hash(control_policy),
            exit_policy_snapshot=control_policy,
        ),
    )


def _quote(at: datetime, bid: float):
    return SimpleNamespace(
        quote_timestamp=at.isoformat(), quote_timestamp_field="quoteTimestamp",
        bid=bid, ask=bid + 0.05, last=bid + 0.02
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _recorder(tmp_path: Path, **kwargs) -> ExitEdgeLiveRecorder:
    return ExitEdgeLiveRecorder(
        db_path=tmp_path / "edge.db",
        status_path=tmp_path / "status.json",
        **kwargs,
    )


def test_live_recorder_pairs_from_reused_quotes_and_stops_observing(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.start()
    assert recorder.try_register_entry(
        deployment=_deployment(),
        trade_id="T1",
        option_symbol=OPTION,
        entry_timestamp=ENTRY,
        entry_premium=2.0,
        quantity=10,
    )
    _wait_until(lambda: recorder.snapshot()["active_cohorts"] == 1)

    for sequence, bid in enumerate([2.10, 2.70, 2.75, 3.40, 3.35], start=1):
        quote_at = ENTRY + timedelta(seconds=15 * sequence)
        recorder.observe_quote(
            OPTION, _quote(quote_at, bid), quote_at + timedelta(milliseconds=100)
        )
    _wait_until(lambda: recorder.snapshot()["paired_cohorts"] == 1)
    snapshot = recorder.snapshot()
    assert snapshot["active_cohorts"] == 0
    assert snapshot["broker_calls_added"] == 0
    recorder.close()

    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    case = repository.load_case("exit-edge:T1")
    assert len(case.quotes) == 5
    assert case.persisted_censor_reason is None
    assert case.legacy_config["comparator_version"] == "bhiksha-native-premium-stop-full-target-eod-v1"
    states = repository.load_shadow_envelope_states("T1")
    assert {state.candidate_id for state in states} == (
        set(SHADOW_CANDIDATE_IDS) - {"control"}
    )


def test_full_queue_and_slow_storage_never_block_quote_observer(tmp_path: Path) -> None:
    class SlowRepository(ProspectiveQuoteTapeRepository):
        def try_register_cohort(self, payload):
            time.sleep(0.25)
            return super().try_register_cohort(payload)

    recorder = _recorder(
        tmp_path, queue_capacity=1, repository_factory=SlowRepository
    )
    recorder.start()
    assert recorder.try_register_entry(
        deployment=_deployment(), trade_id="T1", option_symbol=OPTION,
        entry_timestamp=ENTRY, entry_premium=2.0, quantity=10,
    )
    started = time.perf_counter()
    for index in range(100):
        at = ENTRY + timedelta(seconds=index + 1)
        recorder.observe_quote(OPTION, _quote(at, 2.1), at)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert recorder.snapshot()["dropped_observations"] > 0
    recorder.close(join_timeout_seconds=2.0)
    case = ProspectiveQuoteTapeRepository(tmp_path / "edge.db").load_case("exit-edge:T1")
    assert case.persisted_censor_reason == "quote_queue_full"


def test_registration_queue_full_is_durable_denominator_not_silent_loss(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, queue_capacity=1)
    # First attempt is deliberately ineligible and occupies the bounded queue.
    assert not recorder.try_register_entry(
        deployment=_deployment(), trade_id="T0", option_symbol=OPTION,
        entry_timestamp=None, entry_premium=None, quantity=None,
    )
    # The eligible confirmed fill cannot enqueue, but its attempt is retained
    # outside the full quote queue for durable denominator readback.
    assert not recorder.try_register_entry(
        deployment=_deployment(), trade_id="T1", option_symbol=OPTION,
        entry_timestamp=ENTRY, entry_premium=2.0, quantity=10,
    )
    recorder.start()
    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    _wait_until(lambda: (tmp_path / "edge.db").exists())
    _wait_until(lambda: repository.registration_summary()["confirmed_fill_attempts"] == 2)
    summary = repository.registration_summary()
    assert summary["registered_cohorts"] == 0
    assert summary["missing_or_ineligible_registrations"] == 2
    recorder.close()


def test_registration_summary_is_empty_before_writer_initializes_schema(
    tmp_path: Path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")

    assert repository.registration_summary() == {
        "confirmed_fill_attempts": 0,
        "eligible_attempts": 0,
        "registered_cohorts": 0,
        "missing_or_ineligible_registrations": 0,
    }


def test_registration_summary_waits_for_brief_schema_writer_lock(
    tmp_path: Path,
) -> None:
    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    repository.initialize()
    lock_ready = Event()

    def hold_brief_schema_lock() -> None:
        with sqlite3.connect(repository.path) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            lock_ready.set()
            time.sleep(0.02)
            conn.commit()

    writer = Thread(target=hold_brief_schema_lock)
    writer.start()
    assert lock_ready.wait(timeout=1.0)
    summary = repository.registration_summary()
    writer.join(timeout=1.0)

    assert not writer.is_alive()
    assert summary["confirmed_fill_attempts"] == 0


def test_restart_gap_censors_unfinished_persisted_cohort(tmp_path: Path) -> None:
    builder = _recorder(tmp_path)
    _, payload = builder._registration_payloads(
        deployment=_deployment(), trade_id="T1", option_symbol=OPTION,
        entry_timestamp=ENTRY, entry_premium=2.0, quantity=10,
    )
    assert payload is not None
    repository = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    repository.initialize()
    repository.register_cohort(payload)

    restarted = _recorder(tmp_path)
    restarted.start()
    _wait_until(lambda: restarted.snapshot()["censored_cohorts"] == 1)
    restarted.close()
    case = repository.load_case("exit-edge:T1")
    assert case.persisted_censor_reason == "restart_gap_unobserved_quotes"


def test_flag_defaults_off_and_env_explicitly_enables(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "app.yaml"
    config_path.write_text("app_name: bhiksha\n")
    assert load_app_config(config_path).exit_edge_live_shadow_enabled is False
    monkeypatch.setenv("BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED", "true")
    assert load_app_config(config_path).exit_edge_live_shadow_enabled is True


def test_recorder_module_has_no_order_manager_or_broker_import() -> None:
    source = inspect.getsource(exit_edge_live)
    assert "execution.order_manager" not in source
    assert "execution.brokers" not in source


def test_experiment_fill_facts_never_fall_back_to_plan_like_fields() -> None:
    complete = {
        "averageFillPrice": "2.15", "filledQuantity": "3",
        "closedAt": "2026-07-10T14:01:02Z", "openedAt": "2026-07-10T14:00:00Z",
        "status": "FILLED",
    }
    price, quantity, filled_at = _confirmed_entry_fill_facts(complete)
    assert (price, quantity) == (2.15, 3)
    assert filled_at == datetime(2026, 7, 10, 14, 1, 2, tzinfo=UTC)
    # Requested/estimated facts and openedAt are deliberately ineligible.
    assert _confirmed_entry_fill_facts({
        "price": "2.15", "quantity": "3", "openedAt": "2026-07-10T14:00:00Z",
        "status": "FILLED",
    }) == (None, None, None)
    assert _confirmed_entry_fill_facts({
        **complete, "status": "PARTIALLY_FILLED"
    }) == (None, None, None)


def test_order_manager_tee_adds_no_broker_call_and_is_failure_isolated() -> None:
    class Broker:
        def __init__(self):
            self.calls = 0

        async def get_quotes(self, instruments):
            self.calls += 1
            return {"quotes": [{
                "instrument": {"symbol": OPTION}, "bid": "2.00", "ask": "2.05",
                "last": "2.02", "timestamp": ENTRY.isoformat(),
            }]}

    def broken_observer(*args):
        raise RuntimeError("observer unavailable")

    import asyncio

    broker = Broker()
    manager = OrderManager(broker=broker, quote_observer=broken_observer)
    quote = asyncio.run(manager.get_option_quote(OPTION))
    assert quote.bid == 2.0
    assert broker.calls == 1


def test_quote_timestamp_prefers_bid_ask_timestamp_when_multiple_fields_exist() -> None:
    from bhiksha.execution.order_manager import _quote_timestamp

    value, field = _quote_timestamp({
        "timestamp": "generic", "quoteTimestamp": "bid-ask-specific",
        "lastTradeTime": "trade-time",
    })
    assert (value, field) == ("bid-ask-specific", "quoteTimestamp")


def test_bounded_tee_stays_fast_with_many_same_contract_cohorts(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, queue_capacity=256)
    recorder.start()
    for index in range(16):
        assert recorder.try_register_entry(
            deployment=_deployment(), trade_id=f"T{index}", option_symbol=OPTION,
            entry_timestamp=ENTRY, entry_premium=2.0, quantity=10,
        )
    _wait_until(lambda: recorder.snapshot()["active_cohorts"] == 16)
    started = time.perf_counter()
    for sequence, bid in enumerate([2.10, 2.70, 2.75, 3.40, 3.35], start=1):
        at = ENTRY + timedelta(seconds=15 * sequence)
        recorder.observe_quote(OPTION, _quote(at, bid), at + timedelta(milliseconds=100))
    assert time.perf_counter() - started < 0.05
    _wait_until(lambda: recorder.snapshot()["paired_cohorts"] == 16, timeout=5.0)
    assert recorder.snapshot()["dropped_observations"] == 0
    recorder.close()
