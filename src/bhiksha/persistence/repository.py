"""Persistence interface placeholders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from bhiksha.domain.models import CashBudgetDay, CashBudgetReservation, TradeRecord


class EventRepository(ABC):
    @abstractmethod
    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Persist an event payload."""


class NullEventRepository(EventRepository):
    """No-op event repository for tests or temporary use."""

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


class TradeStateRepository(ABC):
    @abstractmethod
    async def upsert_trade(self, record: TradeRecord) -> None:
        """Create or update the durable trade session."""

    @abstractmethod
    async def mark_closed(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> None:
        """Mark a trade session as closed."""

    @abstractmethod
    async def get_open_trades(self) -> list[TradeRecord]:
        """Return open or pending trade sessions."""

    @abstractmethod
    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        """Return recent trade sessions, including recently closed rows."""


class NullTradeStateRepository(TradeStateRepository):
    async def upsert_trade(self, record: TradeRecord) -> None:
        return None

    async def mark_closed(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> None:
        del (
            exit_price,
            exit_filled_quantity,
            exit_filled_at,
            exit_order_status,
            exit_order_type,
            exit_broker_payload,
            exit_rule,
        )
        return None

    async def get_open_trades(self) -> list[TradeRecord]:
        return []

    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        del limit
        return []


class CashBudgetRepository(ABC):
    @abstractmethod
    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        """Return the stored day budget for the trading date."""

    @abstractmethod
    async def upsert_day(self, day: CashBudgetDay) -> None:
        """Create or update the stored day budget."""

    @abstractmethod
    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        """Return the reservation for the trade when present."""

    @abstractmethod
    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        """Create or update a reservation row."""

    @abstractmethod
    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        """Update a reservation status in place."""

    @abstractmethod
    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        """Return summed reservation amounts by status for the trading date."""


class NullCashBudgetRepository(CashBudgetRepository):
    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        del trade_date
        return None

    async def upsert_day(self, day: CashBudgetDay) -> None:
        del day
        return None

    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        del trade_id
        return None

    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        del reservation
        return None

    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        del trade_id, status
        return None

    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        del trade_date
        return {"reserved": 0.0, "consumed": 0.0}
