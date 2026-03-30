"""In-memory event bus for the Bhiksha runtime."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any


class InMemoryEventBus:
    """Simple pub/sub bus backed by asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[type[Any], list[asyncio.Queue[Any]]] = defaultdict(list)

    def subscribe(self, event_type: type[Any]) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers[event_type].append(queue)
        return queue

    async def iter_events(self, event_type: type[Any]) -> AsyncIterator[Any]:
        queue = self.subscribe(event_type)
        while True:
            yield await queue.get()

    async def publish(self, event: Any) -> None:
        for queue in list(self._subscribers.get(type(event), [])):
            await queue.put(event)
