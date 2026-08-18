from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bhiksha.experiments.cartographer_attempts import (
    ATTEMPT_EVENT,
    OUTCOME_EVENT,
    attempt_outcome_payload,
    attempt_start_payload,
    load_attempt_events,
    recovery_action,
    signal_attempt_id,
    trigger_accounting,
    unresolved_attempts,
)
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.active_plan.runtime import reconcile_cartographer_attempts
from bhiksha.config.models import AppConfig
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.state.position_tracker import PositionTracker
from historical_config import historical_deployment


def _context(*, timestamp: datetime) -> dict[str, object]:
    return {
        "signal_attempt_id": signal_attempt_id(
            deployment_id="mc-v1-demo",
            timestamp=timestamp,
            active_plan_id="plan-1",
            session_id="session-1",
            signal_id="mc-v1-demo",
        ),
        "signal_id": "mc-v1-demo",
        "deployment_id": "mc-v1-demo",
        "symbol": "DEMO",
        "signal_timestamp": timestamp.isoformat(),
        "active_plan_id": "plan-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "cartographer_version": "1.1",
        "profile_slug": "TREND_CONTINUATION",
        "bundle_hash": "sha256:bundle",
        "valid_after": (timestamp - timedelta(minutes=1)).isoformat(),
        "valid_through": (timestamp + timedelta(minutes=5)).isoformat(),
        "execution_mode": "shadow",
    }


def test_attempt_identity_is_stable_and_recovery_is_bounded() -> None:
    observed = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=observed)
    assert context["signal_attempt_id"] == _context(timestamp=observed)["signal_attempt_id"]
    assert recovery_action(
        context,
        now=observed + timedelta(minutes=1),
        shadow_only=True,
        has_existing_entry=False,
    ) == ("replay", "fresh_shadow_recovery")
    assert recovery_action(
        context,
        now=observed + timedelta(minutes=10),
        shadow_only=True,
        has_existing_entry=False,
    ) == ("censor", "chart_entry_observation_stale")
    assert recovery_action(
        context,
        now=observed + timedelta(minutes=2),
        shadow_only=True,
        has_existing_entry=False,
    ) == ("censor", "chart_entry_observation_stale")
    assert recovery_action(
        {**context, "recovery_attempted": True},
        now=observed + timedelta(minutes=1),
        shadow_only=True,
        has_existing_entry=False,
    ) == ("censor", "recovery_already_attempted")
    assert recovery_action(
        context,
        now=observed + timedelta(minutes=1),
        shadow_only=False,
        has_existing_entry=False,
    ) == ("censor", "live_replay_forbidden")


def test_attempt_slice_cannot_be_aged_out_by_later_signal_evaluations(tmp_path) -> None:
    database = tmp_path / "events.db"
    context = _context(timestamp=datetime(2026, 8, 18, 13, 35, tzinfo=UTC))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO events(created_at, event_type, payload) VALUES (?, ?, ?)",
            (context["signal_timestamp"], ATTEMPT_EVENT, json.dumps(attempt_start_payload(context))),
        )
        connection.executemany(
            "INSERT INTO events(created_at, event_type, payload) VALUES (?, ?, ?)",
            [
                (context["signal_timestamp"], "signal_evaluation", json.dumps({"signal": False, "index": index}))
                for index in range(2_500)
            ],
        )
    events = load_attempt_events(database)
    assert any(event["event_type"] == ATTEMPT_EVENT for event in events)
    assert not any(event["event_type"] == "signal_evaluation" for event in events)


def test_sqlite_attempt_start_and_outcome_are_idempotent(tmp_path) -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    async def run() -> None:
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(context, decision_reason=["manual_trigger_met"]))
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(context, decision_reason=["manual_trigger_met"]))
        await repo.append(
            OUTCOME_EVENT,
            attempt_outcome_payload(context, outcome="infrastructure_censored", reason="chart_entry_observation_stale"),
        )
        await repo.append(
            OUTCOME_EVENT,
            attempt_outcome_payload(context, outcome="infrastructure_censored", reason="chart_entry_observation_stale"),
        )

    asyncio.run(run())
    events = load_attempt_events(tmp_path / "events.db")
    assert [event["event_type"] for event in events] == [ATTEMPT_EVENT, OUTCOME_EVENT]
    assert trigger_accounting(events) == {
        "true_triggers": 1,
        "executed_attempts": 0,
        "legitimate_blocks": 0,
        "explicit_failures": 0,
        "infrastructure_censored": 1,
        "accounted": 1,
        "remainder": 0,
        "unresolved_attempt_ids": [],
        "duplicate_outcome_attempt_ids": [],
        "status": "healthy",
    }


def test_legacy_true_signal_is_attention_not_clean_no_trade(tmp_path) -> None:
    database = tmp_path / "events.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO events(created_at, event_type, payload) VALUES (?, ?, ?)",
            (
                "2026-08-18T13:35:00+00:00",
                "signal_decision",
                json.dumps(
                    {
                        "deployment_id": "mc-v1-legacy",
                        "signal": True,
                        "timestamp": "2026-08-18T13:35:00+00:00",
                    }
                ),
            ),
        )
    report = trigger_accounting(load_attempt_events(database))
    assert report["true_triggers"] == 1
    assert report["remainder"] == 1
    assert report["status"] == "attention"


def test_signal_decision_with_attempt_id_is_accounted_if_append_is_interrupted() -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    attempt = _context(timestamp=timestamp)
    events = [{
        "event_type": "signal_decision",
        "payload": {
            "deployment_id": attempt["deployment_id"],
            "signal_attempt_id": attempt["signal_attempt_id"],
            "signal": True,
            "timestamp": attempt["signal_timestamp"],
        },
    }]
    report = trigger_accounting(events)
    assert report["true_triggers"] == 1
    assert report["remainder"] == 1
    assert report["status"] == "attention"
    assert unresolved_attempts(events)[0]["signal_attempt_id"] == attempt["signal_attempt_id"]


def test_frozen_tuesday_cartographer_accounting_is_two_legacy_triggers() -> None:
    events = [
        {
            "event_type": "signal_decision",
            "payload": {
                "deployment_id": "mc-v1-4325b7068a8b9e1097007de7",
                "signal": True,
                "timestamp": "2026-08-18T13:35:12.102151+00:00",
            },
        },
        {
            "event_type": "signal_decision",
            "payload": {
                "deployment_id": "mc-v1-c7c2d95389ddf850708f116f",
                "signal": True,
                "timestamp": "2026-08-18T13:35:12.102151+00:00",
            },
        },
    ]
    report = trigger_accounting(events)
    assert report["true_triggers"] == 2
    assert report["infrastructure_censored"] == 0
    assert report["remainder"] == 2
    assert report["status"] == "attention"


def test_unresolved_attempt_reader_excludes_terminal_outcome() -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    events = [
        {"event_type": ATTEMPT_EVENT, "payload": attempt_start_payload(context)},
        {
            "event_type": OUTCOME_EVENT,
            "payload": attempt_outcome_payload(
                context, outcome="blocked", reason="lifecycle_blocked"
            ),
        },
    ]
    assert unresolved_attempts(events) == []


def test_startup_recovery_censors_stale_attempt_once_without_replay(tmp_path) -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    class _Trades:
        async def get_recent_trades(self, *, limit: int):
            del limit
            return []

    class _Supervisor:
        calls = 0

        async def handle_signal(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1

    supervisor = _Supervisor()
    deployment = SimpleNamespace(
        deployment_id="mc-v1-demo",
        symbol="DEMO",
        execution=SimpleNamespace(shadow_only=True),
    )

    async def run() -> tuple[dict[str, int], dict[str, int]]:
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(
            context, decision_reason=["manual_trigger_met"], direction="long"
        ))
        first = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(hours=1),
            output=lambda message: None,
        )
        second = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(hours=1),
            output=lambda message: None,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["censored"] == 1
    assert second["pending"] == 0
    assert supervisor.calls == 0


def test_startup_recovery_censors_when_existing_entry_state_is_unavailable(tmp_path) -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    class _Trades:
        async def get_recent_trades(self, *, limit: int):
            del limit
            raise RuntimeError("trade state unavailable")

    class _Supervisor:
        calls = 0

        async def handle_signal(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1

    async def run() -> dict[str, int]:
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(
            context, decision_reason=["manual_trigger_met"], direction="long"
        ))
        return await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=_Supervisor(),
            deployments_by_id={"mc-v1-demo": SimpleNamespace(
                deployment_id="mc-v1-demo",
                symbol="DEMO",
                execution=SimpleNamespace(shadow_only=True),
            )},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(minutes=1),
            output=lambda message: None,
        )

    assert asyncio.run(run()) == {
        "pending": 1,
        "replayed": 0,
        "censored": 1,
        "deferred": 0,
    }
    assert unresolved_attempts(load_attempt_events(tmp_path / "events.db")) == []


def test_startup_recovery_closes_persisted_trade_plan_without_replay(tmp_path) -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    class _Trades:
        async def get_recent_trades(self, *, limit: int):
            del limit
            return []

    class _Supervisor:
        calls = 0

        async def handle_signal(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1

    supervisor = _Supervisor()
    deployment = SimpleNamespace(
        deployment_id="mc-v1-demo",
        symbol="DEMO",
        execution=SimpleNamespace(shadow_only=True),
    )
    plan = {
        "signal_attempt_id": context["signal_attempt_id"],
        "trade_id": "trade-after-plan",
        "option_symbol": "DEMO260821C00100",
        "quantity": 1,
        "dry_run": True,
        "order_id": None,
    }

    async def run() -> tuple[dict[str, int], dict[str, int]]:
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(
            context, decision_reason=["manual_trigger_met"], direction="long"
        ))
        await repo.append("trade_plan", plan)
        first = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(minutes=10),
            output=lambda message: None,
        )
        second = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(minutes=10),
            output=lambda message: None,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["pending"] == 1
    assert second["pending"] == 0
    assert supervisor.calls == 0
    outcomes = [
        event for event in load_attempt_events(tmp_path / "events.db")
        if event["event_type"] == OUTCOME_EVENT
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["outcome"] == "execution"
    assert outcomes[0]["payload"]["reason"] == "trade_plan_persisted"


def test_fresh_shadow_recovery_marks_one_replay_then_censors_on_interruption(tmp_path) -> None:
    timestamp = datetime(2026, 8, 18, 13, 35, tzinfo=UTC)
    context = _context(timestamp=timestamp)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))

    class _Trades:
        async def get_recent_trades(self, *, limit: int):
            del limit
            return []

    class _Supervisor:
        calls = 0

        async def handle_signal(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            # Simulate interruption before the normal supervisor can append an
            # outcome.  The recovery marker must prevent a second replay.
            raise RuntimeError("simulated interruption")

    supervisor = _Supervisor()
    deployment = SimpleNamespace(
        deployment_id="mc-v1-demo",
        symbol="DEMO",
        execution=SimpleNamespace(shadow_only=True),
    )

    async def run() -> tuple[dict[str, int], dict[str, int]]:
        await repo.append(ATTEMPT_EVENT, attempt_start_payload(
            context, decision_reason=["manual_trigger_met"], direction="long"
        ))
        first = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(minutes=1),
            output=lambda message: None,
        )
        second = await reconcile_cartographer_attempts(
            events_db_path=tmp_path / "events.db",
            event_repository=repo,
            supervisor=supervisor,
            deployments_by_id={"mc-v1-demo": deployment},
            trade_state_repository=_Trades(),
            live=True,
            now=timestamp + timedelta(minutes=1),
            output=lambda message: None,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["replayed"] == 0
    assert first["censored"] == 1
    assert second["pending"] == 0
    assert supervisor.calls == 1


def test_sheet_failure_is_observable_but_does_not_gate_shadow_planning(tmp_path) -> None:
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "enabled": True,
            "execution": base.execution.model_copy(update={"shadow_only": True}),
            "source": base.source.model_copy(
                update={
                    "origin": "active_sheet_manual",
                    "metadata": {
                        "source_owner": "market_cartographer",
                        "signal_id": "mc-v1-qqq",
                        "run_id": "run-1",
                        "cartographer_version": "1.1",
                        "profile_slug": "TREND_CONTINUATION",
                        "bundle_hash": "sha256:bundle",
                        "valid_after": "2026-08-18T13:30:00+00:00",
                        "valid_through": "2026-08-18T14:00:00+00:00",
                        "row_index": 3,
                        "sheet_name": "manual_entry",
                    },
                }
            ),
        }
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        timestamp=datetime(2026, 8, 18, 13, 35, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["manual_trigger_met"],
        features={"close": 500.0},
    )

    class _Planner:
        def __init__(self) -> None:
            self.position_tracker = PositionTracker()
            self.calls = 0

        async def close(self) -> None:
            return None

        async def plan_entry(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            return TradePlan(
                trade_id="shadow-trade-1",
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=SignalDirection.SHORT,
                option_symbol="QQQ260830P00500000",
                quantity=1,
                estimated_entry_price=2.0,
                risk_reasons=["approved"],
                dry_run=True,
                order_id=None,
                underlying_entry_price=500.0,
                entry_timestamp=decision.timestamp,
            )

    class _FailingStatusWriter:
        async def mark_signal_triggered(self, *args, **kwargs):
            del args, kwargs
            await asyncio.sleep(0.02)
            return "[SSL: RECORD_LAYER_FAILURE]"

        async def mark_entry_planned(self, *args, **kwargs):
            del args, kwargs
            await asyncio.sleep(0.02)
            return "[SSL: RECORD_LAYER_FAILURE]"

    planner = _Planner()
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        manual_status_writer=_FailingStatusWriter(),
    )

    async def run() -> None:
        plan = await supervisor.handle_signal(
            deployment, decision, dry_run=True, simulate_only=True
        )
        assert plan is not None
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert planner.calls == 1
    with sqlite3.connect(tmp_path / "events.db") as connection:
        rows = connection.execute(
            "SELECT event_type, payload FROM events ORDER BY id"
        ).fetchall()
    event_types = [row[0] for row in rows]
    assert event_types.index(ATTEMPT_EVENT) < event_types.index("sheet_status_writeback_failure")
    outcome = [json.loads(payload) for event_type, payload in rows if event_type == OUTCOME_EVENT]
    assert len(outcome) == 1
    assert outcome[0]["outcome"] == "execution"
