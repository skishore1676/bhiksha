"""Base interfaces for market-data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator

from bhiksha.domain.models import Bar


class UnderlyingBarSource(ABC):
    @abstractmethod
    async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        """Load historical 1-minute bars for warm start."""

    @abstractmethod
    async def stream_closed_bars(self, symbols: list[str]) -> AsyncIterator[Bar]:
        """Yield closed 1-minute bars as they become available."""

    @abstractmethod
    async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
        """Fetch the latest completed 1-minute bar for a symbol."""
