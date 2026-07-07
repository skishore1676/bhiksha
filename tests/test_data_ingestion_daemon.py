import asyncio
import time
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


def _bar_for(symbol: str) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(2026, 3, 30, 14, 31, tzinfo=UTC),
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.05,
        volume=1000.0,
    )


def test_data_ingestion_daemon_fetches_and_dispatches_all_symbols_in_one_wake() -> None:
    """All N symbols' bars are fetched and dispatched within a single wake."""

    class MultiSymbolSource:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            self.calls.append(symbol)
            return _bar_for(symbol)

    symbols = ["QQQ", "SPY", "IWM", "DIA", "AAPL"]
    source = MultiSymbolSource()
    bus = InMemoryEventBus()
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=symbols,
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=len(symbols)))
        events = [await queue.get() for _ in symbols]
        await task
        return events

    events = asyncio.run(run())
    assert sorted(event.symbol for event in events) == sorted(symbols)
    assert sorted(source.calls) == sorted(symbols)


def test_data_ingestion_daemon_one_symbol_failure_does_not_block_others() -> None:
    """A single symbol's fetch raising (even a hung/slow one) must not prevent
    the other symbols in the same wake from being fetched and dispatched."""

    class PartiallyFailingSource:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            self.calls.append(symbol)
            if symbol == "IWM":
                request = httpx.Request("GET", "https://example.test")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError("503", request=request, response=response)
            return _bar_for(symbol)

    symbols = ["QQQ", "IWM", "SPY"]
    source = PartiallyFailingSource()
    bus = InMemoryEventBus()
    seen_errors: list[tuple[str, str, str]] = []
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=symbols,
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
        on_fetch_error=lambda symbol, provider, exc: _record_error(seen_errors, symbol, provider, exc),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        # Two of the three symbols will publish a bar; cap on that count.
        task = asyncio.create_task(daemon.run(max_bars=2))
        events = [await queue.get(), await queue.get()]
        await task
        return events

    events = asyncio.run(run())
    assert sorted(event.symbol for event in events) == ["QQQ", "SPY"]
    # The failing symbol was still attempted alongside the others.
    assert sorted(source.calls) == sorted(symbols)
    assert seen_errors == [("IWM", "schwab", "HTTPStatusError")]


def test_data_ingestion_daemon_fetches_concurrently_not_serially() -> None:
    """Per-symbol fetches within a wake overlap in time rather than running
    one-after-another; wall clock should track max(single fetch), not the
    sum across all symbols."""

    class ConcurrencyTrackingSource:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                # Yield control so other concurrently-scheduled fetches get a
                # chance to start before this one finishes. A serial sweep
                # would never observe more than 1 in flight at a time.
                await asyncio.sleep(0.01)
                return _bar_for(symbol)
            finally:
                self.in_flight -= 1

    symbols = [f"SYM{i}" for i in range(6)]
    source = ConcurrencyTrackingSource()
    bus = InMemoryEventBus()
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=symbols,
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=len(symbols)))
        for _ in symbols:
            await queue.get()
        await task

    start = time.perf_counter()
    asyncio.run(run())
    elapsed = time.perf_counter() - start

    assert source.max_in_flight > 1, "expected overlapping fetches, saw a serial sweep"
    # 6 fetches * 10ms would take >= 60ms serially; concurrent should land
    # close to a single fetch's duration with generous headroom for CI jitter.
    assert elapsed < 0.05, f"sweep took {elapsed:.3f}s, looks serial not concurrent"


def test_data_ingestion_daemon_respects_max_concurrent_fetches_bound() -> None:
    """The concurrency bound bounds actual in-flight fetches (courtesy cap
    against provider throttling), even with more symbols than the cap."""

    class ConcurrencyTrackingSource:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.01)
                return _bar_for(symbol)
            finally:
                self.in_flight -= 1

    symbols = [f"SYM{i}" for i in range(10)]
    source = ConcurrencyTrackingSource()
    bus = InMemoryEventBus()
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=symbols,
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
        max_concurrent_fetches=3,
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=len(symbols)))
        for _ in symbols:
            await queue.get()
        await task

    asyncio.run(run())
    assert source.max_in_flight <= 3


def test_data_ingestion_daemon_dispatch_order_matches_symbol_list_order() -> None:
    """Downstream dispatch order follows `self.symbols` order (not fetch
    completion order), matching the serial version's guarantee, even when
    later-listed symbols resolve before earlier-listed ones."""

    class OutOfOrderCompletionSource:
        async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
            # Reverse-ish completion order: earlier-listed symbols finish
            # slower than later-listed ones.
            delay = {"FIRST": 0.03, "SECOND": 0.02, "THIRD": 0.01}[symbol]
            await asyncio.sleep(delay)
            return _bar_for(symbol)

    symbols = ["FIRST", "SECOND", "THIRD"]
    source = OutOfOrderCompletionSource()
    bus = InMemoryEventBus()
    daemon = DataIngestionDaemon(
        source,
        bus,
        symbols=symbols,
        provider="schwab",
        sleep=lambda _: asyncio.sleep(0),
    )
    queue = bus.subscribe(BarClosedEvent)

    async def run():
        task = asyncio.create_task(daemon.run(max_bars=len(symbols)))
        events = [await queue.get() for _ in symbols]
        await task
        return events

    events = asyncio.run(run())
    assert [event.symbol for event in events] == symbols
    for event, symbol in zip(events, symbols):
        assert event.bar.symbol == symbol
        assert event.provider == "schwab"
        assert event.timeframe == "1m"
