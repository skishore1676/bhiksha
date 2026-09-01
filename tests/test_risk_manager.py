from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sqlite3

from bhiksha.domain.models import CashBudgetDay, PartialFillRecord, TradeRecord
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.risk.risk_manager import (
    RiskManager,
    TIER1_HALT_REASON,
    TIER2_FLATTEN_REASON,
    RAIL_B_SESSION_BLOCK_REASON,
    BUDGET_UNAVAILABLE_REASON,
    OPEN_DRAWDOWN_WARNING_REASON,
    CORRELATION_CLUSTER_CAP_REASON,
    PROSPECTIVE_LOSS_HEADROOM_REASON,
    OPEN_POSITION_RISK_UNAVAILABLE_REASON,
    _complete_realized_pnl_usd,
)
from bhiksha.risk.risk_settings import RiskSettings, resolve_risk_settings


class _ManualClock:
    """A settable ``now_fn`` for tests that need to move wall-clock time
    across multiple ``book_actions()`` calls without sleeping."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


def _settings(**overrides) -> RiskSettings:
    base = dict(
        rail_a_enabled=True,
        rail_b_enabled=True,
        max_daily_drawdown_pct=2.0,
        flatten_daily_drawdown_pct=3.0,
        demote_window=10,
        demote_min_n=10,
        demote_threshold_usd=0.0,
    )
    base.update(overrides)
    return RiskSettings(**base)


def _manager(tmp_path, *, settings=None, now=None, alert_mode="off", mark_price_provider=None):
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    return RiskManager(
        settings=settings or _settings(),
        cash_budget_repository=SQLiteCashBudgetRepository(db_path, backend=backend),
        trade_state_repository=SQLiteTradeStateRepository(db_path, backend=backend),
        event_repository=SQLiteEventRepository(db_path, backend=backend),
        alert_mode=alert_mode,
        now_fn=(lambda: now) if now is not None else None,
        mark_price_provider=mark_price_provider,
    ), db_path


def _mark_provider_from(marks: dict[str, float | None]):
    """A mark_price_provider fixture that returns a fixed mark per option_symbol.

    A symbol absent from ``marks`` raises (simulating a quote fetch failure)
    so tests can distinguish "no mark configured" from "mark is None"."""

    async def provider(option_symbol: str) -> float | None:
        if option_symbol not in marks:
            raise ValueError(f"no mark configured for {option_symbol}")
        return marks[option_symbol]

    return provider


def _open_live_trade(
    trade_id: str, *, deployment_id: str, option_symbol: str, entry: float, quantity: int = 1
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        deployment_id=deployment_id,
        symbol="QQQ",
        option_symbol=option_symbol,
        quantity=quantity,
        entry_price=entry,
        entry_timestamp=NOW,
        status="open_unprotected",
        entry_order_id="LIVE123",
    )


def _events(db_path: str, event_type: str | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        if event_type:
            rows = conn.execute(
                "SELECT event_type, payload FROM events WHERE event_type = ? ORDER BY id", (event_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    return [{"event_type": row[0], "payload": json.loads(row[1])} for row in rows]


def _closed_live_trade(trade_id: str, *, deployment_id: str, entry: float, exit_: float, exit_at: datetime, quantity: int = 1) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        deployment_id=deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=quantity,
        entry_price=entry,
        entry_timestamp=exit_at,
        status="closed",
        entry_order_id="LIVE123",
        exit_price=exit_,
        exit_filled_quantity=quantity,
        exit_filled_at=exit_at,
    )


def _confirmed_partial(
    trade_id: str,
    *,
    deployment_id: str,
    entry_at: datetime,
    fill_price: float,
    quantity: int = 1,
) -> PartialFillRecord:
    return PartialFillRecord(
        id=None,
        trade_id=trade_id,
        deployment_id=deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        closed_quantity=quantity,
        order_id=f"PARTIAL-{trade_id}",
        exit_rule="target_1_partial",
        submitted_at=entry_at,
        fill_price=fill_price,
        fill_quantity=quantity,
        filled_at=entry_at,
        order_status="FILLED",
        order_type="LIMIT",
    )


NOW = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Rail A
# --------------------------------------------------------------------------


def test_rail_a_missing_budget_day_is_inactive_and_warns(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.rail_a.reason == "no_cash_budget_day"
    assert result.should_flatten is False
    # Exactly one warning event for the missing-data case -- book_actions()
    # does not double-log an "ok" wrapper on top of the inactive warning.
    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 1
    assert decisions[-1]["payload"]["decision"] == "inactive"
    assert decisions[-1]["payload"]["severity"] == "warning"
    assert decisions[-1]["payload"]["reason"] == "no_cash_budget_day"


def test_rail_a_pnl_query_failure_is_inactive_not_halt(tmp_path, monkeypatch) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=9500.0, buffer_pct=0.05)
        )
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(manager, "_realized_live_pnl_today", boom)

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.rail_a.reason == "pnl_query_failed"
    assert result.should_flatten is False


def test_rail_a_tier1_halts_new_entries_without_flatten(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -2.5% of 10000 = -250, breaches tier1 (-200) but not tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    book_result = asyncio.run(manager.book_actions())
    assert book_result.rail_a.halted is True
    assert book_result.rail_a.flatten is False
    assert book_result.should_flatten is False

    entry_decision = asyncio.run(manager.allow_entry("dep1"))
    assert entry_decision.allowed is False
    assert entry_decision.reason == TIER1_HALT_REASON
    assert entry_decision.rail == "A"


def test_rail_a_tier1_halt_is_session_sticky(tmp_path) -> None:
    """A tier-1 halt must not silently clear mid-session if a later P&L
    recompute happens to land back above threshold (e.g. a late fill
    correction moves the day's realized P&L). Once halted, stays halted for
    the rest of the session -- the same posture as tier-2 flatten and Rail B.
    """
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    first = asyncio.run(manager.allow_entry("dep1"))
    assert first.allowed is False
    assert first.reason == TIER1_HALT_REASON

    # Correct the trade's exit price so realized P&L is now well ABOVE
    # threshold -- a naive re-check would allow entries again.
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=5.0, exit_filled_quantity=1, exit_filled_at=NOW))

    second = asyncio.run(manager.allow_entry("dep1"))
    assert second.allowed is False
    assert second.reason == TIER1_HALT_REASON


def test_rail_a_tier2_breach_reports_should_flatten(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tier1 (-200) and tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.halted is True
    assert result.rail_a.flatten is True
    assert result.should_flatten is True
    assert result.flatten_reason == TIER2_FLATTEN_REASON

    decisions = _events(db_path, "risk_manager_decision")
    assert decisions[-1]["payload"]["decision"] == "flatten"


def test_rail_a_ignores_shadow_trades_in_pnl(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    shadow_trade = TradeRecord(
        trade_id="S1",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=1,
        entry_price=8.0,
        entry_timestamp=NOW,
        status="closed",
        entry_order_id="SHADOW_ENTRY",
        exit_price=1.0,
        exit_filled_quantity=1,
        exit_filled_at=NOW,
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(shadow_trade))
    asyncio.run(manager.trade_state_repository.mark_closed("S1", exit_price=1.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.realized_live_pnl_usd == 0.0
    assert result.rail_a.halted is False


def test_rail_a_ignores_dry_run_trades_in_pnl(tmp_path) -> None:
    manager, _ = _manager(tmp_path, now=NOW)
    _put_budget(manager, 10000.0)
    paper_trade = _closed_live_trade(
        "PAPER",
        deployment_id="dep1",
        entry=8.0,
        exit_=1.0,
        exit_at=NOW,
    )
    paper_trade = replace(paper_trade, entry_order_id="DRY_RUN")
    asyncio.run(manager.trade_state_repository.upsert_trade(paper_trade))

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.realized_live_pnl_usd == 0.0
    assert result.rail_a.halted is False


def test_rail_a_disabled_via_settings_is_inactive(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(rail_a_enabled=False))
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    result = asyncio.run(manager.book_actions())
    assert result.rail_a.active is False
    assert result.rail_a.reason == "rail_a_disabled"


# --------------------------------------------------------------------------
# Operator audit P2 (2026-07-03 finding): unknown budget blocks NEW live
# entries but never flattens existing positions.
# --------------------------------------------------------------------------


def test_allow_entry_blocks_when_no_cash_budget_day(tmp_path) -> None:
    """(a) No cash_budget_days row yet -> allow_entry blocks with
    risk_rail_a_budget_unavailable, and still emits its decision event."""
    manager, db_path = _manager(tmp_path, now=NOW)

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is False
    assert decision.reason == BUDGET_UNAVAILABLE_REASON
    assert decision.rail == "A"

    events = _events(db_path, "risk_manager_decision")
    entry_scoped = [event for event in events if event["payload"].get("deployment_id") == "dep1"]
    assert len(entry_scoped) == 1
    assert entry_scoped[0]["payload"]["decision"] == "blocked"
    assert entry_scoped[0]["payload"]["reason"] == BUDGET_UNAVAILABLE_REASON


def test_allow_entry_blocks_when_budget_query_fails(tmp_path, monkeypatch) -> None:
    """Same block for the other unknown-budget reason: the read itself
    raised, not just "row absent"."""
    manager, db_path = _manager(tmp_path, now=NOW)

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(manager.cash_budget_repository, "get_day", boom)

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is False
    assert decision.reason == BUDGET_UNAVAILABLE_REASON
    assert decision.rail == "A"


def test_allow_entry_allows_once_budget_row_exists(tmp_path) -> None:
    """(b) Once the cash_budget_days row exists (e.g. after the startup
    prefetch or a lazy-create), allow_entry evaluates normally again -- the
    unknown-budget block is not latched."""
    manager, db_path = _manager(tmp_path, now=NOW)

    blocked = asyncio.run(manager.allow_entry("dep1"))
    assert blocked.allowed is False
    assert blocked.reason == BUDGET_UNAVAILABLE_REASON

    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    allowed = asyncio.run(manager.allow_entry("dep1"))
    assert allowed.allowed is True
    assert allowed.reason == "approved"


def test_allow_entry_rail_a_disabled_still_allows_with_no_budget_row(tmp_path) -> None:
    """(c) rail_a_enabled=False must keep allowing entries even with no
    budget row -- only the enabled-but-unknown case blocks."""
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(rail_a_enabled=False))

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is True


# --------------------------------------------------------------------------
# Final sized-entry risk: prospective Rail A headroom + correlation clusters
# --------------------------------------------------------------------------


def _put_budget(manager: RiskManager, usable_budget: float) -> None:
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(
                trade_date="2026-04-20",
                account_type="CASH",
                broker_cash_only_buying_power=usable_budget,
                usable_budget=usable_budget,
                buffer_pct=0.0,
            )
        )
    )


def test_sized_entry_reproduces_smh_amd_headroom_block(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(
            max_daily_drawdown_pct=7.5,
            flatten_daily_drawdown_pct=10.0,
            max_open_positions_per_cluster=0,
        ),
    )
    _put_budget(manager, 11342.81)
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="SMH-LIVE",
                deployment_id="smh-dep",
                symbol="SMH",
                option_symbol="SMH260101P00300000",
                quantity=2,
                entry_price=7.70,
                stop_price=5.005,
                status="open_protected",
                entry_order_id="LIVE-SMH",
            )
        )
    )

    decision = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="AMD-PROPOSED",
            deployment_id="amd-dep",
            symbol="AMD",
            entry_price=10.45,
            quantity=1,
            stop_loss_pct=0.35,
        )
    )

    assert decision.allowed is False
    assert decision.reason == PROSPECTIVE_LOSS_HEADROOM_REASON
    assert decision.details["open_planned_stop_loss_usd"] == 539.0
    assert decision.details["proposed_planned_stop_loss_usd"] == 365.75
    assert decision.details["prospective_total_loss_usd"] == 904.75
    assert decision.details["daily_loss_budget_usd"] == 850.71


def test_sized_entry_cluster_blocks_amd_while_smh_is_open(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(max_daily_drawdown_pct=20.0, flatten_daily_drawdown_pct=25.0),
    )
    _put_budget(manager, 10000.0)
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="SMH-LIVE",
                deployment_id="smh-dep",
                symbol="SMH",
                quantity=1,
                entry_price=5.0,
                stop_price=3.0,
                status="open_protected",
                entry_order_id="LIVE-SMH",
            )
        )
    )

    decision = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="AMD-PROPOSED",
            deployment_id="amd-dep",
            symbol="AMD",
            entry_price=5.0,
            quantity=1,
            stop_loss_pct=0.35,
        )
    )

    assert decision.allowed is False
    assert decision.reason == CORRELATION_CLUSTER_CAP_REASON
    assert decision.details["correlation_cluster"] == "semiconductors"
    assert decision.details["cluster_open_or_reserved_count"] == 1


def test_sized_entry_reservations_are_atomic_for_concurrent_signals(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(
            max_daily_drawdown_pct=10.0,
            flatten_daily_drawdown_pct=15.0,
            max_open_positions_per_cluster=0,
        ),
    )
    _put_budget(manager, 10000.0)

    async def run_both():
        return await asyncio.gather(
            manager.reserve_sized_entry(
                trade_id="ONE",
                deployment_id="one",
                symbol="AAPL",
                entry_price=10.0,
                quantity=1,
                stop_loss_pct=0.60,
            ),
            manager.reserve_sized_entry(
                trade_id="TWO",
                deployment_id="two",
                symbol="MSFT",
                entry_price=10.0,
                quantity=1,
                stop_loss_pct=0.60,
            ),
        )

    decisions = asyncio.run(run_both())

    assert sum(decision.allowed for decision in decisions) == 1
    blocked = next(decision for decision in decisions if not decision.allowed)
    assert blocked.reason == PROSPECTIVE_LOSS_HEADROOM_REASON
    assert blocked.details["pending_reserved_stop_loss_usd"] == 600.0


def test_sized_entry_reservation_survives_risk_manager_restart(tmp_path) -> None:
    settings = _settings(
        max_daily_drawdown_pct=10.0,
        flatten_daily_drawdown_pct=15.0,
        max_open_positions_per_cluster=0,
    )
    first_manager, _ = _manager(tmp_path, now=NOW, settings=settings)
    _put_budget(first_manager, 10000.0)
    first = asyncio.run(
        first_manager.reserve_sized_entry(
            trade_id="BEFORE-RESTART",
            deployment_id="one",
            symbol="AAPL",
            entry_price=10.0,
            quantity=1,
            stop_loss_pct=0.60,
        )
    )

    restarted_manager, _ = _manager(tmp_path, now=NOW + timedelta(minutes=1), settings=settings)
    second = asyncio.run(
        restarted_manager.reserve_sized_entry(
            trade_id="AFTER-RESTART",
            deployment_id="two",
            symbol="MSFT",
            entry_price=10.0,
            quantity=1,
            stop_loss_pct=0.60,
        )
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == PROSPECTIVE_LOSS_HEADROOM_REASON
    assert second.details["pending_reserved_stop_loss_usd"] == 600.0


def test_orphaned_sized_entry_reservation_expires_after_thirty_minutes(tmp_path) -> None:
    settings = _settings(
        max_daily_drawdown_pct=10.0,
        flatten_daily_drawdown_pct=15.0,
        max_open_positions_per_cluster=0,
    )
    manager, db_path = _manager(tmp_path, now=NOW, settings=settings)
    _put_budget(manager, 10000.0)
    asyncio.run(
        manager.reserve_sized_entry(
            trade_id="ORPHAN",
            deployment_id="one",
            symbol="AAPL",
            entry_price=10.0,
            quantity=1,
            stop_loss_pct=0.60,
        )
    )

    restarted_manager, _ = _manager(
        tmp_path,
        now=NOW + timedelta(minutes=31),
        settings=settings,
    )
    decision = asyncio.run(
        restarted_manager.reserve_sized_entry(
            trade_id="AFTER-EXPIRY",
            deployment_id="two",
            symbol="MSFT",
            entry_price=10.0,
            quantity=1,
            stop_loss_pct=0.60,
        )
    )

    assert decision.allowed is True
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM entry_risk_reservations WHERE trade_id = 'ORPHAN'"
        ).fetchone()[0]
    assert status == "expired"


def test_sized_entry_reprice_replaces_its_own_reservation(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(max_daily_drawdown_pct=20.0, flatten_daily_drawdown_pct=25.0),
    )
    _put_budget(manager, 10000.0)

    first = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="SMH-ONE",
            deployment_id="smh-dep",
            symbol="SMH",
            entry_price=5.0,
            quantity=1,
            stop_loss_pct=0.35,
        )
    )
    repriced = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="SMH-ONE",
            deployment_id="smh-dep",
            symbol="SMH",
            entry_price=5.5,
            quantity=1,
            stop_loss_pct=0.35,
        )
    )

    assert first.allowed is True
    assert repriced.allowed is True
    assert repriced.details["cluster_open_or_reserved_count"] == 0
    assert repriced.details["proposed_planned_stop_loss_usd"] == 192.5


def test_sized_entry_unprotected_open_trade_counts_full_premium(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(
            max_daily_drawdown_pct=10.0,
            flatten_daily_drawdown_pct=15.0,
            max_open_positions_per_cluster=0,
        ),
    )
    _put_budget(manager, 10000.0)
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="PENDING",
                deployment_id="pending-dep",
                symbol="AAPL",
                quantity=1,
                entry_price=8.0,
                stop_price=None,
                status="pending_entry_reconcile",
                entry_order_id="LIVE-PENDING",
            )
        )
    )

    decision = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="NEXT",
            deployment_id="next-dep",
            symbol="MSFT",
            entry_price=2.0,
            quantity=1,
            stop_loss_pct=0.50,
        )
    )

    assert decision.allowed is True
    assert decision.details["open_planned_stop_loss_usd"] == 800.0
    assert decision.details["unprotected_full_premium_trade_ids"] == ["PENDING"]
    assert decision.reason == "approved"


def test_sized_entry_blocks_when_open_trade_risk_cannot_be_quantified(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(
            max_daily_drawdown_pct=10.0,
            flatten_daily_drawdown_pct=15.0,
            max_open_positions_per_cluster=0,
        ),
    )
    _put_budget(manager, 10000.0)
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            TradeRecord(
                trade_id="BROKEN-OPEN",
                deployment_id="broken-dep",
                symbol="AAPL",
                quantity=1,
                entry_price=None,
                status="open_unprotected",
                entry_order_id="LIVE-BROKEN",
            )
        )
    )

    decision = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="NEXT",
            deployment_id="next-dep",
            symbol="MSFT",
            entry_price=2.0,
            quantity=1,
            stop_loss_pct=0.50,
        )
    )

    assert decision.allowed is False
    assert decision.reason == OPEN_POSITION_RISK_UNAVAILABLE_REASON
    assert decision.details["unquantifiable_open_trade_ids"] == ["BROKEN-OPEN"]


def test_disabling_prospective_loss_does_not_require_a_stop_or_budget(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(
            prospective_loss_enabled=False,
            max_open_positions_per_cluster=0,
        ),
    )

    decision = asyncio.run(
        manager.reserve_sized_entry(
            trade_id="NO-STOP",
            deployment_id="no-stop-dep",
            symbol="AAPL",
            entry_price=2.0,
            quantity=1,
            stop_loss_pct=None,
        )
    )

    assert decision.allowed is True
    assert decision.details["proposed_planned_stop_loss_usd"] == 0.0


def test_allow_entry_not_blocked_by_pnl_query_failure(tmp_path, monkeypatch) -> None:
    """pnl_query_failed is a DIFFERENT inactive reason than the two budget-
    unavailable ones (the budget row exists and is known; the P&L read
    failed) -- it must NOT trip the new BUDGET_UNAVAILABLE_REASON block."""
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=9500.0, buffer_pct=0.05)
        )
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(manager, "_realized_live_pnl_today", boom)

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.reason != BUDGET_UNAVAILABLE_REASON
    assert decision.allowed is True
    assert decision.reason == "approved"


def test_book_actions_stays_inactive_no_flatten_when_no_budget_row(tmp_path) -> None:
    """(d) The unknown-budget entry block must NOT change book_actions /
    flatten behavior: no budget row -> still inactive, never a flatten,
    regardless of the new allow_entry posture."""
    manager, db_path = _manager(tmp_path, now=NOW)

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.rail_a.reason == "no_cash_budget_day"
    assert result.should_flatten is False
    assert result.flatten_reason is None


# --------------------------------------------------------------------------
# Rail B
# --------------------------------------------------------------------------
#
# Every Rail-B test seeds a large cash_budget_days row first: since P2
# (2026-07-03), allow_entry blocks with risk_rail_a_budget_unavailable when
# Rail A is enabled and the row is missing, which would otherwise mask what
# these tests are actually checking (Rail B's own decision). The budget is
# sized well above any accumulated realized loss in these fixtures so Rail A
# itself never trips tier-1/tier-2 and interferes.


def _seed_large_budget(manager) -> None:
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(
                trade_date="2026-04-20",
                account_type="CASH",
                broker_cash_only_buying_power=1_000_000.0,
                usable_budget=1_000_000.0,
                buffer_pct=0.0,
            )
        )
    )


def test_rail_b_fires_exactly_at_min_n_with_negative_expectancy(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    _seed_large_budget(manager)
    for i in range(9):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    # 9 losing trades: no session block yet (below min_n)
    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True

    # 10th losing trade crosses min_n with negative mean -> session-blocked
    trade10 = _closed_live_trade("T9", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade10))
    asyncio.run(manager.trade_state_repository.mark_closed("T9", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is False
    assert decision.reason == RAIL_B_SESSION_BLOCK_REASON
    assert decision.rail == "B"

    block_events = _events(db_path, "risk_manager_session_block")
    assert len(block_events) == 1
    assert block_events[0]["payload"]["deployment_id"] == "dep1"
    assert block_events[0]["payload"]["window_n"] == 10
    assert block_events[0]["payload"]["mean_pnl_usd"] == -100.0
    assert block_events[0]["payload"]["scope"] == "current_session"
    assert block_events[0]["payload"]["sheet_authorization_changed"] is False


def test_rail_b_backfills_past_unpriced_closed_trade_to_build_complete_window(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0),
    )
    _seed_large_budget(manager)
    for i in range(10):
        trade = _closed_live_trade(
            f"PRICED-{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
        )
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(
            manager.trade_state_repository.mark_closed(
                trade.trade_id, exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW
            )
        )

    # Reproduce a legacy/runtime-corrupt row like the live SMH record: status
    # says closed, but no durable exit economics exist. It is the most recent
    # raw row and must not consume one of Rail B's ten evidence slots.
    unpriced = replace(
        _closed_live_trade(
            "UNPRICED", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
        ),
        exit_price=None,
        exit_filled_quantity=None,
        exit_filled_at=None,
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(unpriced))

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is False
    assert decision.reason == RAIL_B_SESSION_BLOCK_REASON
    block = _events(str(tmp_path / "bhiksha.db"), "risk_manager_session_block")[0]["payload"]
    assert block["window_n"] == 10
    assert block["mean_pnl_usd"] == -100.0
    assert "UNPRICED" not in block["trade_ids"]
    assert set(block["trade_ids"]) == {f"PRICED-{i}" for i in range(10)}


def test_rail_b_does_not_fire_with_positive_expectancy(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    _seed_large_budget(manager)
    for i in range(10):
        # winning trades: exit > entry
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=4.0, exit_=5.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=5.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True


def test_rail_b_counts_confirmed_banked_partial_pnl(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0),
    )
    _seed_large_budget(manager)
    for i in range(10):
        trade = _closed_live_trade(
            f"T{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
        )
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(
            manager.trade_state_repository.mark_closed(
                f"T{i}", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW
            )
        )
    # Nine -$100 trades plus T9's -$100 residual and +$1,100 banked leg
    # produce a +$10 mean. Residual-only calculation would falsely block it.
    asyncio.run(
        manager.trade_state_repository.record_partial_fill(
            _confirmed_partial(
                "T9", deployment_id="dep1", entry_at=NOW, fill_price=16.0
            )
        )
    )

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is True


def test_complete_pnl_counts_duplicate_partial_order_once() -> None:
    trade = _closed_live_trade(
        "DUPLICATE", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
    )
    first = _confirmed_partial(
        trade.trade_id, deployment_id="dep1", entry_at=NOW, fill_price=11.0
    )
    duplicate = replace(first, id=2)

    assert _complete_realized_pnl_usd(trade, [first, duplicate]) == 500.0


def test_complete_pnl_bounds_broker_fill_to_submitted_partial_quantity() -> None:
    trade = _closed_live_trade(
        "OVERSIZED", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
    )
    partial = _confirmed_partial(
        trade.trade_id, deployment_id="dep1", entry_at=NOW, fill_price=16.0
    )
    oversized = replace(partial, fill_quantity=99)

    assert _complete_realized_pnl_usd(trade, [oversized]) == 1000.0


def test_rail_a_counts_confirmed_banked_partial_pnl(tmp_path) -> None:
    manager, _ = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(
                trade_date="2026-04-20",
                account_type="CASH",
                broker_cash_only_buying_power=10_000.0,
                usable_budget=10_000.0,
                buffer_pct=0.0,
            )
        )
    )
    trade = _closed_live_trade(
        "PARTIAL-WINNER", deployment_id="dep1", entry=5.0, exit_=2.0, exit_at=NOW
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(
        manager.trade_state_repository.mark_closed(
            trade.trade_id, exit_price=2.0, exit_filled_quantity=1, exit_filled_at=NOW
        )
    )
    asyncio.run(
        manager.trade_state_repository.record_partial_fill(
            _confirmed_partial(
                trade.trade_id,
                deployment_id="dep1",
                entry_at=NOW,
                fill_price=7.0,
            )
        )
    )

    status = asyncio.run(manager.book_actions()).rail_a

    assert status.realized_live_pnl_usd == -100.0
    assert status.halted is False


def test_rail_a_books_partial_on_fill_day_not_runner_close_day(tmp_path) -> None:
    manager, _ = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(
                trade_date="2026-04-20",
                account_type="CASH",
                broker_cash_only_buying_power=10_000.0,
                usable_budget=10_000.0,
                buffer_pct=0.0,
            )
        )
    )
    trade = TradeRecord(
        trade_id="OPEN-PARTIAL",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=1,
        entry_price=5.0,
        entry_timestamp=NOW - timedelta(days=1),
        status="target_active",
        entry_order_id="LIVE123",
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(
        manager.trade_state_repository.record_partial_fill(
            _confirmed_partial(
                trade.trade_id,
                deployment_id="dep1",
                entry_at=NOW,
                fill_price=7.0,
            )
        )
    )

    status = asyncio.run(manager.book_actions()).rail_a

    assert status.realized_live_pnl_usd == 200.0


def test_rail_b_re_evaluates_the_same_window_in_a_new_session(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0),
    )
    _seed_large_budget(manager)
    for i in range(10):
        trade = _closed_live_trade(
            f"OLD{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW
        )
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))

    first = asyncio.run(manager.allow_entry("dep1"))
    assert first.allowed is False

    # A fresh runtime has no hidden persistent mode override. If the Sheet
    # still compiles the lane live, Rail B evaluates the evidence again and
    # may refuse that new session's entry again.
    fresh_manager, fresh_db_path = _manager(
        tmp_path,
        now=NOW + timedelta(minutes=1),
        settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0),
    )
    second = asyncio.run(fresh_manager.allow_entry("dep1"))
    assert second.allowed is False
    assert second.reason == RAIL_B_SESSION_BLOCK_REASON
    assert len(_events(fresh_db_path, "risk_manager_session_block")) == 2


def test_rail_b_is_one_way_no_flap(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    _seed_large_budget(manager)
    for i in range(10):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    first = asyncio.run(manager.allow_entry("dep1"))
    assert first.allowed is False

    # Even if subsequent (hypothetical) trades would look better, add 10 winners
    # and confirm the deployment stays blocked for this session.
    for i in range(10, 20):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=4.0, exit_=6.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=6.0, exit_filled_quantity=1, exit_filled_at=NOW))

    second = asyncio.run(manager.allow_entry("dep1"))
    assert second.allowed is False
    assert second.reason == RAIL_B_SESSION_BLOCK_REASON

    block_events = _events(db_path, "risk_manager_session_block")
    assert len(block_events) == 1  # session latch suppresses repeat alerts


def test_rail_b_only_counts_live_closed_trades_for_this_deployment(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=3, demote_threshold_usd=0.0))
    _seed_large_budget(manager)
    # Losing trades for a DIFFERENT deployment should not affect dep1.
    for i in range(5):
        trade = _closed_live_trade(f"OTHER{i}", deployment_id="dep2", entry=5.0, exit_=1.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"OTHER{i}", exit_price=1.0, exit_filled_quantity=1, exit_filled_at=NOW))
    # An open (not closed) live trade for dep1 should not count.
    open_trade = TradeRecord(
        trade_id="OPEN1",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=1,
        entry_price=5.0,
        entry_timestamp=NOW,
        status="open_unprotected",
        entry_order_id="LIVE999",
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(open_trade))
    # 3 winning closed live trades for dep1.
    for i in range(3):
        trade = _closed_live_trade(f"WIN{i}", deployment_id="dep1", entry=4.0, exit_=6.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"WIN{i}", exit_price=6.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True


def test_rail_b_disabled_via_settings_never_demotes(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(rail_b_enabled=False, demote_min_n=1))
    _seed_large_budget(manager)
    trade = _closed_live_trade("T0", deployment_id="dep1", entry=5.0, exit_=0.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T0", exit_price=0.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True


# --------------------------------------------------------------------------
# Proof surface: risk_manager_decision fires on every consult
# --------------------------------------------------------------------------


def test_allow_entry_always_emits_decision_event_even_when_allowed(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    # A budget row must exist for allow_entry to reach "allowed" -- see
    # test_allow_entry_blocks_when_no_cash_budget_day for the unknown-budget
    # case (P2, 2026-07-03).
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is True
    assert decision.reason == "approved"
    events = _events(db_path, "risk_manager_decision")
    # One deployment-scoped final entry decision: the production proof
    # surface fires on every consult, even a fully "allowed, nothing
    # blocked" one.
    entry_scoped = [event for event in events if event["payload"].get("deployment_id") == "dep1"]
    assert len(entry_scoped) == 1
    assert entry_scoped[0]["payload"]["decision"] == "allowed"


def test_startup_log_emits_resolved_settings(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(max_daily_drawdown_pct=1.5))

    asyncio.run(manager.startup_log())

    events = _events(db_path, "risk_manager_startup")
    assert len(events) == 1
    assert events[0]["payload"]["max_daily_drawdown_pct"] == 1.5
    assert events[0]["payload"]["rail_a_enabled"] is True


# --------------------------------------------------------------------------
# Settings resolution: env precedence
# --------------------------------------------------------------------------


def test_resolve_risk_settings_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "1.25")
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "2.5")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_WINDOW", "20")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_MIN_N", "15")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_THRESHOLD_USD", "-5")
    monkeypatch.setenv("BHIKSHA_RISK_RAIL_A_ENABLED", "false")
    monkeypatch.setenv("BHIKSHA_RISK_RAIL_B_ENABLED", "0")
    monkeypatch.setenv("BHIKSHA_RISK_PROSPECTIVE_LOSS_ENABLED", "false")
    monkeypatch.setenv("BHIKSHA_RISK_MAX_OPEN_POSITIONS_PER_CLUSTER", "2")

    settings = resolve_risk_settings()

    assert settings.max_daily_drawdown_pct == 1.25
    assert settings.flatten_daily_drawdown_pct == 2.5
    assert settings.demote_window == 20
    assert settings.demote_min_n == 15
    assert settings.demote_threshold_usd == -5.0
    assert settings.rail_a_enabled is False
    assert settings.rail_b_enabled is False
    assert settings.prospective_loss_enabled is False
    assert settings.max_open_positions_per_cluster == 2


def test_resolve_risk_settings_defaults_when_no_env(monkeypatch) -> None:
    for key in (
        "BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_DEMOTE_WINDOW",
        "BHIKSHA_RISK_DEMOTE_MIN_N",
        "BHIKSHA_RISK_DEMOTE_THRESHOLD_USD",
        "BHIKSHA_RISK_RAIL_A_ENABLED",
        "BHIKSHA_RISK_RAIL_B_ENABLED",
        "BHIKSHA_RISK_PROSPECTIVE_LOSS_ENABLED",
        "BHIKSHA_RISK_MAX_OPEN_POSITIONS_PER_CLUSTER",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = resolve_risk_settings()

    assert settings.max_daily_drawdown_pct == 2.0
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert settings.demote_window == 10
    assert settings.demote_min_n == 10
    assert settings.demote_threshold_usd == 0.0
    assert settings.rail_a_enabled is True
    assert settings.rail_b_enabled is True
    assert settings.prospective_loss_enabled is True
    assert settings.max_open_positions_per_cluster == 1


# --------------------------------------------------------------------------
# 2026-07-02 adversarial-audit fixes
# --------------------------------------------------------------------------


def test_allow_entry_blocked_when_only_tier2_breached(tmp_path) -> None:
    """Audit finding #1: a flattening book must never accept new entries.

    With inverted tiers (flatten smaller than halt — reachable pre-validation
    or via a future settings source), a loss can breach tier-2 without
    breaching tier-1. Entries must still be blocked.
    """
    inverted = _settings(max_daily_drawdown_pct=3.0, flatten_daily_drawdown_pct=2.0)
    manager, db_path = _manager(tmp_path, settings=inverted, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -2.5% of 10000 = -250: breaches flatten (-200) but not halt (-300).
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    book = asyncio.run(manager.book_actions())
    assert book.rail_a.flatten is True
    assert book.rail_a.halted is False

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is False


def test_resolve_settings_clamps_inverted_tiers(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "3.0")
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "2.0")
    settings = resolve_risk_settings()
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert any("clamping flatten" in w for w in settings.validation_warnings)


def test_resolve_settings_rejects_negative_percentages(monkeypatch) -> None:
    """Audit finding #2: a negative pct flipped the threshold sign and would
    flatten a healthy, flat book. Negative values must fall back to defaults
    with a visible warning."""
    monkeypatch.delenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", raising=False)
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "-3.0")
    settings = resolve_risk_settings()
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert any("must be > 0" in w for w in settings.validation_warnings)


def test_resolve_settings_warns_on_unparseable_env(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "two percent")
    settings = resolve_risk_settings()
    assert settings.max_daily_drawdown_pct == 2.0
    assert any("not a valid number" in w for w in settings.validation_warnings)


def test_resolve_settings_clamps_min_n_to_window(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_WINDOW", "5")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_MIN_N", "10")
    settings = resolve_risk_settings()
    assert settings.demote_min_n == 5
    assert any("clamping min_n" in w for w in settings.validation_warnings)


def test_startup_log_carries_validation_warnings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "-1")
    settings = resolve_risk_settings()
    manager, db_path = _manager(tmp_path, settings=settings, now=NOW)
    asyncio.run(manager.startup_log())
    startup = _events(db_path, "risk_manager_startup")
    assert startup and startup[-1]["payload"]["validation_warnings"]


def test_trade_sessions_updated_at_index_created(tmp_path) -> None:
    """Audit perf finding: get_recent_trades must not full-table-scan."""
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    repo = SQLiteTradeStateRepository(db_path, backend=backend)
    asyncio.run(repo.get_recent_trades(limit=1))  # triggers lazy _init_db
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_trade_sessions_updated_at'"
        ).fetchall()
    assert rows


# --------------------------------------------------------------------------
# 2026-07-02 risk-noise: book_actions() evaluate-throttle + ok-event policy
#
# book_actions() is called once per SYMBOL-bar from BhikshaRuntime (13
# symbols/minute in production) but Rail A is a book-level check. These
# tests cover: (1) the per-minute evaluate cache returns the SAME result
# without recomputing within a minute and DOES recompute across minutes;
# (2) non-ok decisions and decision/state changes always emit a
# risk_manager_decision row uncapped; (3) repeated "ok" decisions are
# capped to one heartbeat row per 10 minutes.
# --------------------------------------------------------------------------


def _manager_with_clock(tmp_path, clock: _ManualClock, *, settings=None, alert_mode="off", mark_price_provider=None):
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    return RiskManager(
        settings=settings or _settings(),
        cash_budget_repository=SQLiteCashBudgetRepository(db_path, backend=backend),
        trade_state_repository=SQLiteTradeStateRepository(db_path, backend=backend),
        event_repository=SQLiteEventRepository(db_path, backend=backend),
        alert_mode=alert_mode,
        now_fn=clock,
        mark_price_provider=mark_price_provider,
    ), db_path


def test_book_actions_returns_cached_result_within_same_minute(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    first = asyncio.run(manager.book_actions())
    # 12 more calls within the SAME minute -- simulating the 13-symbol tick
    # fan-out this is meant to collapse.
    for _ in range(12):
        clock.advance(seconds=1)
        result = asyncio.run(manager.book_actions())
        assert result is first  # literally the cached object, not recomputed

    # Exactly one "ok" decision event was emitted for all 13 calls.
    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision"] == "ok"


def test_book_actions_reevaluates_across_minute_boundary(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    first = asyncio.run(manager.book_actions())
    clock.advance(minutes=1)
    second = asyncio.run(manager.book_actions())

    assert second is not first
    assert second.rail_a.realized_live_pnl_usd == first.rail_a.realized_live_pnl_usd  # same book, recomputed fresh


def test_book_actions_flatten_latch_unaffected_by_cache(tmp_path) -> None:
    """The should_flatten session latch must stay fired even though the
    triggering evaluation itself is now cached per-minute."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tier1 (-200) and tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())
    assert result.should_flatten is True

    # A "late fill correction" within the SAME minute that would look
    # healthy again must not un-flatten -- but this call also hits the
    # evaluate cache, so it returns the SAME cached flattened result.
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=8.0, exit_filled_quantity=1, exit_filled_at=NOW))
    still_cached = asyncio.run(manager.book_actions())
    assert still_cached.should_flatten is True

    # Advance past the minute boundary: recompute happens, P&L now looks
    # healthy, but the session latch must still hold (once flattened, never
    # un-flattened this session).
    clock.advance(minutes=1)
    recomputed = asyncio.run(manager.book_actions())
    assert recomputed.should_flatten is True


def test_book_actions_non_ok_decisions_always_emit_uncapped(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tiers -> every re-evaluation reports "flatten".
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    # 5 evaluations, each in a DIFFERENT minute (so the evaluate-cache does
    # not collapse them) -- every one must emit, well under the 10-minute
    # "ok" heartbeat window that would otherwise cap them.
    for _ in range(5):
        asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 5
    assert all(d["payload"]["decision"] == "flatten" for d in decisions)


def test_book_actions_ok_decision_emits_on_state_change(tmp_path) -> None:
    """A halt->ok or ok->halt transition must emit immediately even if it
    happens well inside the 10-minute ok-heartbeat window."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    # Minute 0: healthy book -> "ok".
    first = asyncio.run(manager.book_actions())
    assert first.rail_a.reason is None

    # Minute 1: book now breaches tier1 -> "halt". Must emit even though
    # only 1 minute has passed since the last "ok" heartbeat.
    clock.advance(minutes=1)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))
    second = asyncio.run(manager.book_actions())
    assert second.rail_a.halted is True

    decisions = _events(db_path, "risk_manager_decision")
    assert [d["payload"]["decision"] for d in decisions] == ["ok", "halt"]


def test_book_actions_ok_heartbeat_emits_once_per_ten_minutes(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    # 15 one-minute ticks, all steady "ok" -- expect a heartbeat at minute 0
    # and minute 10 only (the 10-minute window), i.e. 2 rows, not 15.
    for _ in range(15):
        asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert all(d["payload"]["decision"] == "ok" for d in decisions)
    assert len(decisions) == 2


def test_book_actions_net_daily_ok_row_volume_is_low(tmp_path) -> None:
    """Sanity check on the net effect: ~6.5 evaluation hours (13 symbols x 1
    call/min for 390 minutes each, collapsed by the minute cache to 390
    evaluations) of a steady healthy book should produce ~40 ok-rows/day
    (one per 10-minute heartbeat), not ~5000."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    trading_day_minutes = 390  # one 6.5h US equity/options session
    symbols_per_minute = 13
    for _minute in range(trading_day_minutes):
        for _symbol in range(symbols_per_minute):
            asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert all(d["payload"]["decision"] == "ok" for d in decisions)
    # 390 minutes / 10-minute heartbeat -> 39 rows (minute 0, 10, ..., 380).
    assert len(decisions) == 39


# --------------------------------------------------------------------------
# Operator audit P4 (2026-07-06): open-book mark-to-market WARNING.
#
# Rail A is realized-P&L-only -- it will not notice an open live position
# bleeding intraday until the loss is realized. Native protective stops still
# guard every trade (this is an awareness gap, not a naked-risk gap). These
# tests cover: correct unrealized-P&L math from open live positions + marks
# (profitable and losing); combined day-MTM breach emits exactly ONE warning
# event (latched, not per-tick); no warning when Rail A has already
# realized-halted; missing marks/budget/provider -> no warning (fail-safe);
# sheet/env/default precedence for the new knob including default-to-tier1.
# --------------------------------------------------------------------------


def test_open_drawdown_no_warning_when_mark_price_provider_not_wired(tmp_path) -> None:
    """Feature is INACTIVE (never a spurious warning) when the runtime hasn't
    wired a mark_price_provider -- e.g. an older/partial construction path."""
    manager, db_path = _manager(tmp_path, now=NOW)  # no mark_price_provider
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0)
        )
    )

    result = asyncio.run(manager.book_actions())

    assert result.open_drawdown is not None
    assert result.open_drawdown.active is False
    assert result.open_drawdown.reason == "mark_price_provider_unavailable"
    warnings = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert warnings == []


def test_open_drawdown_computes_unrealized_pnl_for_long_profitable_position(tmp_path) -> None:
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 7.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0, quantity=2)
        )
    )

    result = asyncio.run(manager.book_actions())

    # (7.0 - 5.0) * 2 * 100 = +400
    assert result.open_drawdown.active is True
    assert result.open_drawdown.unrealized_open_usd == 400.0
    assert result.open_drawdown.realized_usd == 0.0
    assert result.open_drawdown.day_mtm_usd == 400.0
    assert result.open_drawdown.breached is False
    assert result.open_drawdown.open_position_count == 1


def test_open_drawdown_computes_unrealized_pnl_for_losing_position(tmp_path) -> None:
    """'Short and long' framing (coordinator spec): this book is always-long-
    premium (buy calls/puts), so the interesting sign case is a LOSING long
    position (mark below entry), not an actual short-position formula."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101P00500000": 2.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101P00500000", entry=5.0, quantity=1)
        )
    )

    result = asyncio.run(manager.book_actions())

    # (2.0 - 5.0) * 1 * 100 = -300
    assert result.open_drawdown.unrealized_open_usd == -300.0
    assert result.open_drawdown.day_mtm_usd == -300.0


def test_open_drawdown_breach_emits_exactly_one_latched_warning(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(
        tmp_path, clock, mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0})
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # (1.0 - 5.0) * 4 * 100 = -1600 unrealized; -1600/10000 = -16% >> the
    # default warn pct (falls back to tier-1's 2.0%, threshold -200).
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0, quantity=4)
        )
    )

    first = asyncio.run(manager.book_actions())
    assert first.open_drawdown.breached is True
    assert first.open_drawdown.day_mtm_usd == -1600.0
    assert first.open_drawdown.warn_pct == 2.0  # defaulted to max_daily_drawdown_pct
    assert first.open_drawdown.warn_threshold_usd == -200.0

    warnings = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert len(warnings) == 1
    payload = warnings[0]["payload"]
    assert payload["realized_usd"] == 0.0
    assert payload["unrealized_open_usd"] == -1600.0
    assert payload["day_mtm_usd"] == -1600.0
    assert payload["open_position_count"] == 1

    # A second tick (still breached, next minute so the evaluate-cache
    # doesn't just return the same cached object) must NOT emit a second
    # warning event -- latched once per session, matching tier1/tier2.
    clock.advance(minutes=1)
    second = asyncio.run(manager.book_actions())
    assert second.open_drawdown.breached is True
    warnings_after = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert len(warnings_after) == 1


def test_open_drawdown_no_warning_when_rail_a_already_realized_halted(tmp_path) -> None:
    """When realized-only Rail A has ALREADY halted (tier-1), the open-book
    warning is specifically about unrealized pushing past the line BEFORE
    realized does -- so it must not also fire here (avoid double-signaling
    the same underlying "book is in trouble" fact via two separate events).
    """
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # Realized loss alone already breaches tier-1 (-250 <= -200).
    closed = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(closed))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))
    # Also an open position bleeding further, which alone would breach the
    # open-book warning threshold too.
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0, quantity=4)
        )
    )

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.halted is True  # realized-only Rail A already tripped
    assert result.open_drawdown.active is True
    assert result.open_drawdown.breached is False  # suppressed -- already signaled via Rail A halt
    warnings = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert warnings == []


def test_open_drawdown_no_warning_when_a_position_mark_is_missing(tmp_path) -> None:
    """Fail-safe: a mark fetch failure for one open position excludes it from
    the sum (never estimates/guesses); with only ONE open position and its
    mark missing, priced_count is 0 -> the whole check is inactive, never a
    spurious warning built from zero real data."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({}),  # no marks configured -> always raises
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0)
        )
    )

    result = asyncio.run(manager.book_actions())

    assert result.open_drawdown.active is False
    assert result.open_drawdown.reason == "no_priced_open_positions"
    warnings = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert warnings == []


def test_open_drawdown_partial_marks_excludes_only_the_missing_position(tmp_path) -> None:
    """Two open positions, one with a mark and one without: the priced one
    still contributes to the sum, the unpriced one is simply excluded (not
    estimated), and open_position_count reflects only the priced one."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0, quantity=1)
        )
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O2", deployment_id="dep1", option_symbol="NO_MARK_SYMBOL", entry=3.0, quantity=1)
        )
    )

    result = asyncio.run(manager.book_actions())

    assert result.open_drawdown.active is True
    assert result.open_drawdown.open_position_count == 1
    # (1.0 - 5.0) * 1 * 100 = -400 -- only the priced position contributes.
    assert result.open_drawdown.unrealized_open_usd == -400.0


def test_open_drawdown_no_warning_when_no_cash_budget_day(tmp_path) -> None:
    """Missing budget -> Rail A itself is inactive, so the open-book check is
    never even evaluated (book_actions only computes it when Rail A is
    active) -- fail-safe, never a spurious warning built on an unknown budget."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0}),
    )
    asyncio.run(
        manager.trade_state_repository.upsert_trade(
            _open_live_trade("O1", deployment_id="dep1", option_symbol="QQQ260101C00500000", entry=5.0, quantity=4)
        )
    )

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.open_drawdown is None
    warnings = _events(db_path, OPEN_DRAWDOWN_WARNING_REASON)
    assert warnings == []


def test_open_drawdown_no_open_positions_still_evaluates_realized_only(tmp_path) -> None:
    """No open live positions: unrealized is unambiguously zero (no marks to
    fetch), but the day-MTM check still runs against realized-only so a
    warning is never silently skipped just because the book happens to be
    flat right now."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -1.5% of 10000 = -150: below the default 2.0% warn threshold (-200), so
    # NOT breached -- proves the realized-only path is evaluated correctly
    # (not just "no open positions -> skip").
    closed = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=3.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(closed))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=3.5, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())

    assert result.open_drawdown.active is True
    assert result.open_drawdown.open_position_count == 0
    assert result.open_drawdown.unrealized_open_usd == 0.0
    assert result.open_drawdown.day_mtm_usd == -150.0
    assert result.open_drawdown.breached is False


def test_open_drawdown_shadow_open_positions_excluded_from_unrealized(tmp_path) -> None:
    """Shadow/dry-run open positions must not contribute to the open-book
    unrealized sum, mirroring Rail A's realized-P&L live/shadow filter."""
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    shadow_open = TradeRecord(
        trade_id="S1",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=10,
        entry_price=5.0,
        entry_timestamp=NOW,
        status="open_unprotected",
        entry_order_id="SHADOW_ENTRY",
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(shadow_open))

    result = asyncio.run(manager.book_actions())

    assert result.open_drawdown.active is True
    assert result.open_drawdown.open_position_count == 0
    assert result.open_drawdown.unrealized_open_usd == 0.0


def test_open_drawdown_rail_a_disabled_is_inactive(tmp_path) -> None:
    manager, db_path = _manager(
        tmp_path,
        now=NOW,
        settings=_settings(rail_a_enabled=False),
        mark_price_provider=_mark_provider_from({"QQQ260101C00500000": 1.0}),
    )
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    result = asyncio.run(manager.book_actions())

    # Rail A disabled -> _compute_rail_a_status returns active=False, so
    # book_actions never even reaches _compute_open_drawdown_status.
    assert result.open_drawdown is None


# --------------------------------------------------------------------------
# Operator audit P4: open_drawdown_warn_pct settings precedence, including
# "unset -> falls back to max_daily_drawdown_pct" (RiskManager.
# effective_open_drawdown_warn_pct), tested both via the resolver AND via a
# directly-constructed RiskSettings (the two paths must agree).
# --------------------------------------------------------------------------


def test_resolve_open_drawdown_warn_pct_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", "0.75")
    settings = resolve_risk_settings()
    assert settings.open_drawdown_warn_pct == 0.75


def test_resolve_open_drawdown_warn_pct_unset_stays_none_on_settings(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", raising=False)
    settings = resolve_risk_settings()
    assert settings.open_drawdown_warn_pct is None


def test_effective_open_drawdown_warn_pct_falls_back_to_tier1_when_unset(tmp_path) -> None:
    manager, _ = _manager(tmp_path, settings=_settings(max_daily_drawdown_pct=1.25))
    assert manager.settings.open_drawdown_warn_pct is None
    assert manager.effective_open_drawdown_warn_pct == 1.25


def test_effective_open_drawdown_warn_pct_uses_explicit_value_when_set(tmp_path) -> None:
    manager, _ = _manager(
        tmp_path, settings=_settings(max_daily_drawdown_pct=2.0, open_drawdown_warn_pct=0.5)
    )
    assert manager.effective_open_drawdown_warn_pct == 0.5


def test_resolve_open_drawdown_warn_pct_rejects_non_positive(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", "0")
    settings = resolve_risk_settings()
    assert settings.open_drawdown_warn_pct is None
    assert any("open_drawdown_warn_pct" in w for w in settings.validation_warnings)


def test_resolve_open_drawdown_warn_pct_warns_on_unparseable_env(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_OPEN_DRAWDOWN_WARN_PCT", "not-a-number")
    settings = resolve_risk_settings()
    assert settings.open_drawdown_warn_pct is None
    assert any("not a valid number" in w for w in settings.validation_warnings)


def test_startup_log_carries_open_drawdown_warn_pct(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(open_drawdown_warn_pct=0.8))
    asyncio.run(manager.startup_log())
    startup = _events(db_path, "risk_manager_startup")
    assert startup[-1]["payload"]["open_drawdown_warn_pct"] == 0.8
