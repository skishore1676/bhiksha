import asyncio
from datetime import UTC, datetime

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import BarClosedEvent
from bhiksha.domain.models import Bar


def test_in_memory_event_bus_publishes_to_subscribers() -> None:
    bus = InMemoryEventBus()
    queue = bus.subscribe(BarClosedEvent)
    event = BarClosedEvent(
        symbol="QQQ",
        timeframe="1m",
        provider="schwab",
        bar=Bar(
            symbol="QQQ",
            timestamp=datetime(2026, 3, 30, 14, 31, tzinfo=UTC),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=1000.0,
        ),
    )

    async def run():
        await bus.publish(event)
        return await queue.get()

    received = asyncio.run(run())
    assert received == event
