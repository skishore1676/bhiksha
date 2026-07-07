"""Runtime-owned market-data heartbeat daemon."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

import httpx

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import BarClosedEvent
from bhiksha.market_data.adapters.base import UnderlyingBarSource


class DataIngestionDaemon:
    """Fetch completed bars on a precise heartbeat and publish BarClosedEvent."""

    # Bound on concurrent in-flight fetches per wake. Neither the Schwab nor
    # Public bar-source clients configure custom httpx connection-pool limits
    # (httpx defaults to 100 max connections), and PublicApiClient already
    # self-throttles every request through an internal token-bucket
    # RateLimiter, so full symbol-count concurrency (~13 today) would be
    # safe. A modest cap of 8 is kept anyway as a courtesy bound against
    # provider-side throttling/connection churn as the symbol list grows,
    # without meaningfully limiting the wall-clock win (sweep time becomes
    # ~max(single fetch) rather than sum, same as uncapped, for realistic
    # symbol counts).
    MAX_CONCURRENT_FETCHES = 8

    def __init__(
        self,
        source: UnderlyingBarSource,
        event_bus: InMemoryEventBus,
        *,
        symbols: list[str],
        provider: str,
        heartbeat_second: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_fetch_error: Callable[[str, str, Exception], Awaitable[None]] | None = None,
        on_fetch_recovered: Callable[[str, str], Awaitable[None]] | None = None,
        max_concurrent_fetches: int | None = None,
    ) -> None:
        self.source = source
        self.event_bus = event_bus
        self.symbols = symbols
        self.provider = provider
        self.heartbeat_second = heartbeat_second
        self._sleep = sleep
        self._on_fetch_error = on_fetch_error
        self._on_fetch_recovered = on_fetch_recovered
        self._stopped = False
        self._last_seen: dict[str, datetime] = {}
        self._degraded_symbols: set[str] = set()
        self._max_concurrent_fetches = max_concurrent_fetches or self.MAX_CONCURRENT_FETCHES

    def stop(self) -> None:
        self._stopped = True

    async def run(self, *, max_bars: int | None = None) -> int:
        published = 0
        while not self._stopped:
            await self._sleep(self.seconds_until_heartbeat(datetime.now(UTC), self.heartbeat_second))
            now = datetime.now(UTC)
            semaphore = asyncio.Semaphore(self._max_concurrent_fetches)

            async def fetch_one(symbol: str) -> object:
                async with semaphore:
                    try:
                        return await self.source.fetch_latest_completed_bar(symbol, now=now)
                    except Exception as exc:  # noqa: BLE001 - classified below
                        return exc

            # Fire all per-symbol fetches for this wake concurrently instead
            # of sweeping serially; wall-clock becomes ~max(single fetch)
            # rather than sum(all fetches). gather() preserves input order
            # regardless of completion order, so `results` lines up 1:1 with
            # `self.symbols` (a plain list, not a dict, so duplicate symbols
            # -- none exist today, deployments are unique per-symbol -- would
            # still each get their own fetch and result slot). Results are
            # then processed/dispatched sequentially in that same order so
            # downstream ordering guarantees are unchanged from the serial
            # version.
            results = await asyncio.gather(*(fetch_one(symbol) for symbol in self.symbols))

            for symbol, outcome in zip(self.symbols, results):
                if self._stopped:
                    break
                if isinstance(outcome, Exception):
                    exc = outcome
                    if _is_retryable_provider_error(exc):
                        self._degraded_symbols.add(symbol)
                        if self._on_fetch_error is not None:
                            await self._on_fetch_error(symbol, self.provider, exc)
                        continue
                    raise exc
                bar = outcome
                if symbol in self._degraded_symbols:
                    self._degraded_symbols.discard(symbol)
                    if self._on_fetch_recovered is not None:
                        await self._on_fetch_recovered(symbol, self.provider)
                if bar is None:
                    continue
                previous = self._last_seen.get(symbol)
                if previous is not None and bar.timestamp <= previous:
                    continue
                self._last_seen[symbol] = bar.timestamp
                await self.event_bus.publish(
                    BarClosedEvent(
                        symbol=symbol,
                        timeframe="1m",
                        provider=self.provider,
                        bar=bar,
                    )
                )
                published += 1
                if max_bars is not None and published >= max_bars:
                    self._stopped = True
                    break
        return published

    @staticmethod
    def seconds_until_heartbeat(now: datetime, heartbeat_second: int) -> float:
        current = now.astimezone(UTC)
        next_tick = current.replace(second=heartbeat_second, microsecond=0)
        if next_tick <= current:
            next_tick = (current + timedelta(minutes=1)).replace(second=heartbeat_second, microsecond=0)
        return max((next_tick - current).total_seconds(), 0.0)


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False
