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

    def stop(self) -> None:
        self._stopped = True

    async def run(self, *, max_bars: int | None = None) -> int:
        published = 0
        while not self._stopped:
            await self._sleep(self.seconds_until_heartbeat(datetime.now(UTC), self.heartbeat_second))
            now = datetime.now(UTC)
            for symbol in self.symbols:
                if self._stopped:
                    break
                try:
                    bar = await self.source.fetch_latest_completed_bar(symbol, now=now)
                except Exception as exc:
                    if _is_retryable_provider_error(exc):
                        self._degraded_symbols.add(symbol)
                        if self._on_fetch_error is not None:
                            await self._on_fetch_error(symbol, self.provider, exc)
                        continue
                    raise
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
