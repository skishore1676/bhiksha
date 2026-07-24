from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from bhiksha.domain.exit_state import (
    ExitActionIntent,
    ExitRuntimeState,
    TradeExitPolicySnapshot,
)
from bhiksha.persistence.exit_state import SQLiteExitStateRepository
from bhiksha.execution.exit_policy import canonical_policy_hash


POLICY = {"policy_id": "exit.test.v1"}
POLICY_HASH = canonical_policy_hash(POLICY)


def _snapshot() -> TradeExitPolicySnapshot:
    return TradeExitPolicySnapshot(
        trade_id="T1",
        deployment_id="D1",
        option_symbol="QQQ_OPT",
        active_plan_id="active_plan_2026-07-24",
        startup_config_id="cfg",
        policy_schema_version="exit-policy.v1",
        policy_id="exit.test.v1",
        policy_hash=POLICY_HASH,
        canonical_policy=POLICY,
        provenance={"resolution": "source_explicit"},
        frozen_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _state() -> ExitRuntimeState:
    return ExitRuntimeState(
        trade_id="T1",
        deployment_id="D1",
        option_symbol="QQQ_OPT",
        policy_hash=POLICY_HASH,
        seed_entry_premium=2.69,
        seed_quantity=5,
        initial_risk_per_contract=0.9415,
        raw_peak_premium=2.69,
        confirmed_peak_r=0.0,
        committed_stop_price=1.75,
        state_version=1,
    )


def test_snapshot_and_initial_state_are_atomic_idempotent_and_immutable(
    tmp_path,
) -> None:
    repo = SQLiteExitStateRepository(str(tmp_path / "state.db"))
    asyncio.run(repo.freeze_policy_and_initialize_state(_snapshot(), _state()))
    asyncio.run(repo.freeze_policy_and_initialize_state(_snapshot(), _state()))
    assert asyncio.run(repo.get_policy_snapshot("T1")) == _snapshot()
    assert asyncio.run(repo.get_runtime_state("T1")) == _state()

    conflicting = replace(_snapshot(), policy_hash="b" * 64)
    with pytest.raises(ValueError, match="hash does not match"):
        asyncio.run(
            repo.freeze_policy_and_initialize_state(
                conflicting,
                replace(_state(), policy_hash="b" * 64),
            )
        )
    assert asyncio.run(repo.get_policy_snapshot("T1")).policy_hash == POLICY_HASH


def test_runtime_transition_is_versioned_and_monotonic(tmp_path) -> None:
    repo = SQLiteExitStateRepository(str(tmp_path / "state.db"))
    asyncio.run(repo.freeze_policy_and_initialize_state(_snapshot(), _state()))
    advanced = replace(
        _state(),
        raw_peak_premium=3.44,
        confirmed_peak_r=0.7966,
        peak_timestamp=datetime(2026, 7, 20, 15, tzinfo=UTC),
        state_version=2,
    )
    asyncio.run(repo.transition_runtime_state(advanced, expected_version=1))
    assert asyncio.run(repo.get_runtime_state("T1")) == advanced

    with pytest.raises(ValueError, match="version conflict"):
        asyncio.run(
            repo.transition_runtime_state(
                advanced,
                expected_version=1,
            )
        )
    with pytest.raises(ValueError, match="raw peak premium cannot regress"):
        asyncio.run(
            repo.transition_runtime_state(
                replace(advanced, raw_peak_premium=3.0, state_version=3),
                expected_version=2,
            )
        )


def test_prepared_action_intent_blocks_duplicate_slot_across_restart(
    tmp_path,
) -> None:
    db = str(tmp_path / "state.db")
    repo = SQLiteExitStateRepository(db)
    asyncio.run(repo.freeze_policy_and_initialize_state(_snapshot(), _state()))
    intent = ExitActionIntent(
        idempotency_key="T1:target_1:v1",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1",
        expected_state_version=1,
        requested_quantity=3,
    )
    asyncio.run(repo.prepare_action_intent(intent))
    restarted = SQLiteExitStateRepository(db)
    open_intents = asyncio.run(restarted.get_open_action_intents("T1"))
    assert len(open_intents) == 1
    assert open_intents[0].idempotency_key == intent.idempotency_key
    assert open_intents[0].requested_quantity == 3
    assert open_intents[0].status == "prepared"

    asyncio.run(
        restarted.bind_action_order(
            intent.idempotency_key,
            broker_order_id="ORDER1",
            broker_payload={"status": "NEW"},
        )
    )
    assert asyncio.run(restarted.get_open_action_intents("T1"))[0].status == (
        "submitted"
    )
    asyncio.run(
        restarted.resolve_action_intent(
            intent.idempotency_key,
            status="confirmed",
            broker_payload={"status": "FILLED"},
        )
    )
    assert asyncio.run(restarted.get_open_action_intents("T1")) == []

    conflicting = replace(intent, idempotency_key="other", requested_quantity=2)
    with pytest.raises(ValueError, match="different intent"):
        asyncio.run(restarted.prepare_action_intent(conflicting))


def test_action_intent_must_match_frozen_policy_and_current_state_version(
    tmp_path,
) -> None:
    repo = SQLiteExitStateRepository(str(tmp_path / "state.db"))
    asyncio.run(repo.freeze_policy_and_initialize_state(_snapshot(), _state()))
    base = ExitActionIntent(
        idempotency_key="intent",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1",
        expected_state_version=1,
        requested_quantity=2,
    )
    with pytest.raises(ValueError, match="policy hash conflict"):
        asyncio.run(
            repo.prepare_action_intent(
                replace(base, idempotency_key="bad-hash", policy_hash="b" * 64)
            )
        )
    with pytest.raises(ValueError, match="state version conflict"):
        asyncio.run(
            repo.prepare_action_intent(
                replace(base, idempotency_key="stale", expected_state_version=0)
            )
        )
