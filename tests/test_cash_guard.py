import asyncio
from datetime import UTC, datetime

from bhiksha.domain.models import CashBudgetDay, CashBudgetReservation, TradeRecord
from bhiksha.persistence.repository import CashBudgetRepository
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


def test_cash_guard_serializes_concurrent_reservations(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    repository = SlowInMemoryCashBudgetRepository()
    guard = CashGuard(
        order_manager=StubOrderManager(account_type="CASH", cash_only_buying_power="1000.00"),
        repository=repository,
    )
    timestamp = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)

    async def run():
        return await asyncio.gather(
            guard.reserve_entry(trade_id="T1", required_cash=600.0, timestamp=timestamp),
            guard.reserve_entry(trade_id="T2", required_cash=600.0, timestamp=timestamp),
        )

    results = asyncio.run(run())

    approved = [result for result in results if not result.blocked]
    blocked = [result for result in results if result.blocked]
    assert len(approved) == 1
    assert len(blocked) == 1
    assert blocked[0].reason == "insufficient_internal_settled_cash_budget"
    assert repository.active_total("2026-04-20") == 600.0


class SlowInMemoryCashBudgetRepository(CashBudgetRepository):
    def __init__(self) -> None:
        self.days: dict[str, CashBudgetDay] = {}
        self.reservations: dict[str, CashBudgetReservation] = {}

    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        return self.days.get(trade_date)

    async def upsert_day(self, day: CashBudgetDay) -> None:
        self.days[day.trade_date] = day

    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        return self.reservations.get(trade_id)

    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        await asyncio.sleep(0)
        self.reservations[reservation.trade_id] = reservation

    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        reservation = self.reservations[trade_id]
        self.reservations[trade_id] = CashBudgetReservation(
            trade_id=reservation.trade_id,
            trade_date=reservation.trade_date,
            amount=reservation.amount,
            status=status,
        )

    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        await asyncio.sleep(0)
        return {
            "reserved": sum(
                reservation.amount
                for reservation in self.reservations.values()
                if reservation.trade_date == trade_date and reservation.status == "reserved"
            ),
            "consumed": sum(
                reservation.amount
                for reservation in self.reservations.values()
                if reservation.trade_date == trade_date and reservation.status == "consumed"
            ),
        }

    def active_total(self, trade_date: str) -> float:
        return sum(
            reservation.amount
            for reservation in self.reservations.values()
            if reservation.trade_date == trade_date and reservation.status in {"reserved", "consumed"}
        )
