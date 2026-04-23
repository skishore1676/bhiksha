import asyncio
from datetime import UTC, datetime

from bhiksha.domain.models import TradeRecord
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository
from bhiksha.risk.cash_guard import CashGuard
from bhiksha.state.position_tracker import TrackedPosition


class StubOrderManager:
    def __init__(self, *, account_type: str = "CASH", cash_only_buying_power: str = "1000.00") -> None:
        self.account_type = account_type
        self.cash_only_buying_power = cash_only_buying_power

    async def get_account_info(self):
        return {"brokerageAccountType": self.account_type}

    async def get_portfolio(self):
        return {
            "buyingPower": {
                "cashOnlyBuyingPower": self.cash_only_buying_power,
            }
        }


def _guard(tmp_path, *, account_type: str = "CASH", cash_only_buying_power: str = "1000.00") -> CashGuard:
    backend = SQLiteBackend(str(tmp_path / "bhiksha.db"))
    return CashGuard(
        order_manager=StubOrderManager(account_type=account_type, cash_only_buying_power=cash_only_buying_power),
        repository=SQLiteCashBudgetRepository(str(tmp_path / "bhiksha.db"), backend=backend),
    )


def test_cash_guard_reserves_and_releases_daily_budget(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    guard = _guard(tmp_path)
    timestamp = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    first = asyncio.run(guard.reserve_entry(trade_id="T1", required_cash=400.0, timestamp=timestamp))
    blocked = asyncio.run(guard.reserve_entry(trade_id="T2", required_cash=600.0, timestamp=timestamp))
    asyncio.run(guard.release_entry("T1"))
    after_release = asyncio.run(guard.reserve_entry(trade_id="T3", required_cash=600.0, timestamp=timestamp))

    assert first.blocked is False
    assert first.details["usable_budget"] == 950.0
    assert blocked.blocked is True
    assert blocked.reason == "insufficient_internal_settled_cash_budget"
    assert blocked.details["remaining_budget"] == 550.0
    assert after_release.blocked is False


def test_cash_guard_sync_positions_backfills_consumed_budget(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    guard = _guard(tmp_path)
    timestamp = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    asyncio.run(
        guard.sync_positions(
            [
                TrackedPosition(
                    symbol="QQQ",
                    deployment_id="dep1",
                    trade_id="T1",
                    option_symbol="QQQ260330P00558000",
                    quantity=1,
                    entry_price=2.9,
                    entry_timestamp=timestamp,
                )
            ],
            [],
        )
    )
    result = asyncio.run(guard.reserve_entry(trade_id="T2", required_cash=700.0, timestamp=timestamp))
    trade_pending = TradeRecord(
        trade_id="T3",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.9,
        entry_timestamp=timestamp,
        status="pending_entry",
    )
    asyncio.run(guard.sync_positions([], [trade_pending]))
    blocked = asyncio.run(guard.reserve_entry(trade_id="T4", required_cash=400.0, timestamp=timestamp))

    assert result.blocked is True
    assert result.details["remaining_budget"] == 660.0
    assert blocked.blocked is True
    assert blocked.details["remaining_budget"] == 370.0


def test_cash_guard_sync_positions_counts_pending_entry_reconcile(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    guard = _guard(tmp_path)
    timestamp = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    trade_pending_reconcile = TradeRecord(
        trade_id="T5",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.9,
        entry_timestamp=timestamp,
        status="pending_entry_reconcile",
    )

    asyncio.run(guard.sync_positions([], [trade_pending_reconcile]))
    blocked = asyncio.run(guard.reserve_entry(trade_id="T6", required_cash=700.0, timestamp=timestamp))

    assert blocked.blocked is True
    assert blocked.details["remaining_budget"] == 660.0
