from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import sqlite3

from bhiksha.domain.models import TradeRecord
from bhiksha.persistence.sqlite import (
    SQLiteBackend,
    SQLiteCashBudgetRepository,
    SQLiteEventRepository,
    SQLiteTradeStateRepository,
)
from bhiksha.risk.canary_inhibition_store import CanaryInhibitionStore
from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.risk.risk_manager import (
    CANARY_INHIBITED_REASON,
    CANARY_INHIBITION_STATE_UNAVAILABLE_REASON,
    RAIL_B_DEMOTED_REASON,
    RiskManager,
)
from bhiksha.risk.risk_settings import RiskSettings


def _manager(
    tmp_path,
    *,
    inhibition_store: CanaryInhibitionStore,
    demotion_store: DemotionStore | None = None,
    rail_b_enabled: bool = False,
    canary_policies: dict[str, dict[str, object]] | None = None,
    zero_fill_provider=None,
    now_fn=None,
    protection_provider=None,
) -> tuple[RiskManager, str]:
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    manager = RiskManager(
        settings=RiskSettings(
            rail_a_enabled=False,
            rail_b_enabled=rail_b_enabled,
            max_daily_drawdown_pct=2.0,
            flatten_daily_drawdown_pct=3.0,
            demote_window=10,
            demote_min_n=10,
            demote_threshold_usd=0.0,
        ),
        cash_budget_repository=SQLiteCashBudgetRepository(
            db_path, backend=backend
        ),
        trade_state_repository=SQLiteTradeStateRepository(
            db_path, backend=backend
        ),
        event_repository=SQLiteEventRepository(db_path, backend=backend),
        demotion_store=demotion_store
        or DemotionStore(tmp_path / "demotions.json"),
        canary_inhibition_store=inhibition_store,
        canary_policies=canary_policies,
        canary_zero_fill_evidence_provider=zero_fill_provider,
        canary_protection_evidence_provider=protection_provider,
        now_fn=now_fn or (lambda: datetime(2026, 8, 4, 15, tzinfo=UTC)),
        alert_mode="off",
    )
    return manager, db_path


def _decision_events(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE event_type='risk_manager_decision' ORDER BY id"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_allow_entry_blocks_persisted_latch_after_manager_restart(tmp_path) -> None:
    store_path = tmp_path / "canary_inhibitions.json"
    first_manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(store_path),
    )
    assert asyncio.run(first_manager.allow_entry("pdd_live_canary")).allowed is True

    CanaryInhibitionStore(store_path).record_inhibition(
        deployment_id="pdd_live_canary",
        canary_id="pdd-v1",
        reason="provider_overlap_below_floor",
    )
    restarted, db_path = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(store_path),
    )

    decision = asyncio.run(restarted.allow_entry("pdd_live_canary"))

    assert decision.allowed is False
    assert decision.reason == CANARY_INHIBITED_REASON
    assert decision.rail == "CANARY"
    assert decision.details["canary_ids"] == ["pdd-v1"]
    assert _decision_events(db_path)[-1]["reason"] == CANARY_INHIBITED_REASON
    assert asyncio.run(restarted.allow_entry("unrelated_live_lane")).allowed is True


def test_allow_entry_fails_closed_when_latch_store_is_unreadable(tmp_path) -> None:
    store_path = tmp_path / "canary_inhibitions.json"
    store_path.write_text("{broken", encoding="utf-8")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(store_path),
    )

    decision = asyncio.run(manager.allow_entry("pdd_live_canary"))

    assert decision.allowed is False
    assert decision.reason == CANARY_INHIBITION_STATE_UNAVAILABLE_REASON
    assert decision.rail == "CANARY"


def test_existing_rail_b_demotion_behavior_is_unchanged(tmp_path) -> None:
    demotions = DemotionStore(tmp_path / "demotions.json")
    demotions.record_demotion(
        deployment_id="existing_live_lane",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-25.0,
        threshold_usd=0.0,
        trade_ids=[f"T{i}" for i in range(10)],
    )
    manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(
            tmp_path / "canary_inhibitions.json"
        ),
        demotion_store=demotions,
        rail_b_enabled=True,
    )

    decision = asyncio.run(manager.allow_entry("existing_live_lane"))

    assert decision.allowed is False
    assert decision.reason == RAIL_B_DEMOTED_REASON
    assert decision.rail == "B"


def _pdd_policy() -> dict[str, dict[str, object]]:
    return {
        "pdd_live_canary": {
            "canary_id": "pdd-v1",
            "start_at": "2026-08-03T00:00:00-05:00",
            "expires_at": "2026-08-28T15:15:00-05:00",
            "stop_loss_pct": 0.35,
            "max_cumulative_loss_r": -2.0,
        }
    }


def test_allow_entry_creates_durable_unprotected_position_latch(tmp_path) -> None:
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=store,
        canary_policies=_pdd_policy(),
    )

    async def run() -> None:
        await manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="PDD-OPEN",
                deployment_id="pdd_live_canary",
                symbol="PDD",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 8, 3, 14, tzinfo=UTC),
                status="open_unprotected",
                entry_order_id="ENTRY-PDD",
            )
        )
        decision = await manager.allow_entry("pdd_live_canary")
        assert decision.allowed is False
        assert decision.reason == CANARY_INHIBITED_REASON

    asyncio.run(run())
    assert store.matching("pdd_live_canary")[0].reason == "unprotected_position"


def test_open_canary_requires_broker_working_stop_proof(tmp_path) -> None:
    async def working_stop(trade: TradeRecord) -> bool:
        return trade.stop_order_id == "STOP-PDD"

    manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(
            tmp_path / "canary_inhibitions.json"
        ),
        canary_policies=_pdd_policy(),
        protection_provider=working_stop,
    )

    async def run() -> None:
        await manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="PDD-PROTECTED",
                deployment_id="pdd_live_canary",
                symbol="PDD",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 8, 3, 14, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY-PDD",
                stop_order_id="STOP-PDD",
            )
        )
        assert (await manager.allow_entry("pdd_live_canary")).allowed is True

    asyncio.run(run())


def test_allow_entry_latches_missing_exit_attribution(tmp_path) -> None:
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=store,
        canary_policies=_pdd_policy(),
    )

    async def run() -> None:
        await manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="PDD-MISSING",
                deployment_id="pdd_live_canary",
                symbol="PDD",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 8, 3, 14, tzinfo=UTC),
                status="closed",
                entry_order_id="ENTRY-PDD",
            )
        )
        decision = await manager.allow_entry("pdd_live_canary")
        assert decision.allowed is False

    asyncio.run(run())
    assert (
        store.matching("pdd_live_canary")[0].reason
        == "missing_trade_or_exit_attribution"
    )


def test_confirmed_cancelled_zero_fill_is_not_a_canary_trade(tmp_path) -> None:
    async def confirmed_zero_fill(trade_id: str) -> bool:
        return trade_id == "PDD-NOFILL"

    manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(
            tmp_path / "canary_inhibitions.json"
        ),
        canary_policies=_pdd_policy(),
        zero_fill_provider=confirmed_zero_fill,
    )

    async def run() -> None:
        await manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="PDD-NOFILL",
                deployment_id="pdd_live_canary",
                symbol="PDD",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 8, 3, 14, tzinfo=UTC),
                status="closed",
                entry_order_id="ENTRY-PDD",
            )
        )
        assert (await manager.allow_entry("pdd_live_canary")).allowed is True

    asyncio.run(run())


def test_two_complete_negative_one_r_closes_create_loss_latch(tmp_path) -> None:
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=store,
        canary_policies=_pdd_policy(),
    )

    async def run() -> None:
        for index in (1, 2):
            await manager.trade_state_repository.upsert_trade(
                TradeRecord(
                    trade_id=f"PDD-LOSS-{index}",
                    deployment_id="pdd_live_canary",
                    symbol="PDD",
                    quantity=1,
                    entry_price=1.0,
                    entry_timestamp=datetime(
                        2026, 8, 3, 14 + index, tzinfo=UTC
                    ),
                    status="closed",
                    entry_order_id=f"ENTRY-PDD-{index}",
                    exit_order_id=f"EXIT-PDD-{index}",
                    exit_price=0.65,
                    exit_filled_quantity=1,
                    exit_filled_at=datetime(
                        2026, 8, 3, 14 + index, 30, tzinfo=UTC
                    ),
                    exit_order_status="FILLED",
                    exit_rule="profile:stop_loss",
                    frozen_entry_risk_usd=35.0,
                    frozen_round_trip_cost_usd=2.0,
                )
            )
        decision = await manager.allow_entry("pdd_live_canary")
        assert decision.allowed is False

    asyncio.run(run())
    latch = store.matching("pdd_live_canary")[0]
    assert latch.reason == "cumulative_loss_r"
    assert latch.evidence["cumulative_r"] == -2.114286


def test_stale_plan_cannot_enter_after_canary_expiry(tmp_path) -> None:
    policies = _pdd_policy()
    policies["pdd_live_canary"]["expires_at"] = (
        "2026-08-03T15:15:00-05:00"
    )
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=store,
        canary_policies=policies,
    )

    decision = asyncio.run(manager.allow_entry("pdd_live_canary"))

    assert decision.allowed is False
    assert store.matching("pdd_live_canary")[0].reason == (
        "authorization_window_inactive"
    )


def test_final_sized_entry_lock_rechecks_canary_expiry(tmp_path) -> None:
    clock = [datetime(2026, 8, 4, 15, tzinfo=UTC)]
    manager, _ = _manager(
        tmp_path,
        inhibition_store=CanaryInhibitionStore(
            tmp_path / "canary_inhibitions.json"
        ),
        canary_policies=_pdd_policy(),
        now_fn=lambda: clock[0],
    )

    async def run() -> None:
        assert (await manager.allow_entry("pdd_live_canary")).allowed is True
        clock[0] = datetime(2026, 8, 29, 15, tzinfo=UTC)
        decision = await manager.reserve_sized_entry(
            trade_id="PDD-LATE",
            deployment_id="pdd_live_canary",
            symbol="PDD",
            entry_price=1.0,
            quantity=1,
            stop_loss_pct=0.35,
        )
        assert decision.allowed is False
        assert decision.reason == CANARY_INHIBITED_REASON

    asyncio.run(run())


def test_final_sized_entry_lock_rechecks_concurrent_durable_latch(tmp_path) -> None:
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    manager, _ = _manager(
        tmp_path,
        inhibition_store=store,
        canary_policies=_pdd_policy(),
    )

    async def run() -> None:
        assert (await manager.allow_entry("pdd_live_canary")).allowed is True
        store.record_inhibition(
            deployment_id="pdd_live_canary",
            canary_id="pdd-v1",
            reason="operator_stop",
        )
        decision = await manager.reserve_sized_entry(
            trade_id="PDD-STOPPED",
            deployment_id="pdd_live_canary",
            symbol="PDD",
            entry_price=1.0,
            quantity=1,
            stop_loss_pct=0.35,
        )
        assert decision.allowed is False
        assert decision.reason == CANARY_INHIBITED_REASON

    asyncio.run(run())
