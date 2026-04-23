import asyncio
from datetime import UTC, datetime

import httpx

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


def test_data_ingestion_daemon_treats_retryable_provider_error_as_non_fatal() -> None:
    class FlakyBarSource(StubBarSource):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            self.calls += 1
            if self.calls == 1:
                request = httpx.Request("GET", "https://example.test")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("429", request=request, response=response)
            return await super().fetch_latest_completed_bar(symbol, now=now)

    source = FlakyBarSource()
    bus = InMemoryEventBus()
    seen_errors: list[tuple[str, str, str]] = []
    seen_recoveries: list[tuple[str, str]] = []
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=["QQQ"],
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
        on_fetch_error=lambda symbol, provider, exc: _record_error(seen_errors, symbol, provider, exc),
        on_fetch_recovered=lambda symbol, provider: _record_recovery(seen_recoveries, symbol, provider),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=1))
        event = await queue.get()
        await task
        return event

    event = asyncio.run(run())

    assert event.symbol == "QQQ"
    assert seen_errors == [("QQQ", "schwab", "HTTPStatusError")]
    assert seen_recoveries == [("QQQ", "schwab")]


async def _record_error(seen_errors: list[tuple[str, str, str]], symbol: str, provider: str, exc: Exception) -> None:
    seen_errors.append((symbol, provider, type(exc).__name__))


async def _record_recovery(seen_recoveries: list[tuple[str, str]], symbol: str, provider: str) -> None:
    seen_recoveries.append((symbol, provider))
