"""Broker interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BrokerAdapter(ABC):
    @abstractmethod
    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Place an order with the execution broker."""

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        """Fetch current broker positions for reconciliation."""

