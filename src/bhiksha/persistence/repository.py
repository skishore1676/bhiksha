"""Persistence interface placeholders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class EventRepository(ABC):
    @abstractmethod
    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Persist an event payload."""


class NullEventRepository(EventRepository):
    """No-op event repository for tests or temporary use."""

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        return None
