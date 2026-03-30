"""Persistence interface placeholders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from bhiksha.domain.models import TradeRecord


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
    async def mark_closed(self, trade_id: str, *, exit_order_id: str | None = None) -> None:
        """Mark a trade session as closed."""

    @abstractmethod
    async def get_open_trades(self) -> list[TradeRecord]:
        """Return open or pending trade sessions."""


class NullTradeStateRepository(TradeStateRepository):
    async def upsert_trade(self, record: TradeRecord) -> None:
        return None

    async def mark_closed(self, trade_id: str, *, exit_order_id: str | None = None) -> None:
        return None

    async def get_open_trades(self) -> list[TradeRecord]:
        return []
