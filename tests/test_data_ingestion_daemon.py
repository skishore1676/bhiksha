import asyncio
from datetime import UTC, datetime

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import BarClosedEvent
from bhiksha.domain.models import Bar
from bhiksha.market_data.daemon import DataIngestionDaemon


class StubBarSource:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
        self.calls += 1
        return Bar(
            symbol=symbol,
            timestamp=datetime(2026, 3, 30, 14, 31, tzinfo=UTC),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=1000.0,
        )


def test_data_ingestion_daemon_seconds_until_heartbeat_aligns_to_second_one() -> None:
    now = datetime(2026, 3, 30, 14, 30, 45, 500000, tzinfo=UTC)
    seconds = DataIngestionDaemon.seconds_until_heartbeat(now, 1)
    assert round(seconds, 1) == 15.5


def test_data_ingestion_daemon_publishes_bar_closed_event() -> None:
    source = StubBarSource()
    bus = InMemoryEventBus()
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=["QQQ"],
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=1))
        event = await queue.get()
        await task
        return event

    event = asyncio.run(run())
    assert event.symbol == "QQQ"
    assert event.provider == "schwab"
    assert source.calls == 1
