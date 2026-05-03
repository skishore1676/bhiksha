"""Schwab market-data adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

from bhiksha.domain.models import Bar
from bhiksha.integrations.schwab.client import SchwabApiClient
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.market_data.adapters.base import UnderlyingBarSource
from bhiksha.market_data.session import ensure_utc


class SchwabBarSource(UnderlyingBarSource):
    """Warm-start and future live-bar entry point for Schwab market data."""

    def __init__(
        self,
        client: SchwabApiClient | None = None,
        settings: SchwabSettings | None = None,
        poll_interval_seconds: int = 15,
    ) -> None:
        self.settings = settings or SchwabSettings.from_env()
        self.client = client or SchwabApiClient(self.settings)
        self.poll_interval_seconds = poll_interval_seconds

    async def close(self) -> None:
        await self.client.close()

    async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        payload = await self.client.price_history(
            symbol,
            period_type="day",
            period=None,
            frequency_type="minute",
            frequency=1,
            start_date=ensure_utc(start),
            end_date=ensure_utc(end),
            need_extended_hours_data=False,
        )
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        bars: list[Bar] = []
        for candle in payload.get("candles", []):
            bar = self._bar_from_candle(symbol, candle)
            if start_utc <= bar.timestamp <= end_utc:
                bars.append(bar)
        return bars

    async def stream_closed_bars(self, symbols: list[str]) -> AsyncIterator[Bar]:
        last_seen: dict[str, datetime] = {}
        while True:
            for symbol in symbols:
                bar = await self.fetch_latest_completed_bar(symbol)
                if bar is None:
                    continue
                previous = last_seen.get(symbol)
                if previous is None or bar.timestamp > previous:
                    last_seen[symbol] = bar.timestamp
                    yield bar
            await asyncio.sleep(self.poll_interval_seconds)

    async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
        end = now or datetime.now(UTC)
        start = end - timedelta(minutes=5)
        payload = await self.client.price_history(
            symbol,
            period_type="day",
            period=1,
            frequency_type="minute",
            frequency=1,
            start_date=start,
            end_date=end,
            need_extended_hours_data=False,
        )
        return self._latest_completed_bar(symbol, payload.get("candles", []), now=end)

    async def fetch_live_price(self, symbol: str) -> tuple[float, datetime] | None:
        payload = await self.client.quote(symbol)
        quote_payload = payload.get(symbol, {})
        quote = quote_payload.get("quote", {})
        regular = quote_payload.get("regular", {})
        extended = quote_payload.get("extended", {})

        price = _first_float(
            quote.get("mark"),
            quote.get("lastPrice"),
            regular.get("regularMarketLastPrice"),
            extended.get("lastPrice"),
        )
        timestamp_ms = _first_int(
            quote.get("quoteTime"),
            quote.get("tradeTime"),
            regular.get("regularMarketTradeTime"),
            extended.get("tradeTime"),
        )
        if price is None or timestamp_ms is None:
            return None
        return price, ensure_utc(datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC))

    @staticmethod
    def _bar_from_candle(symbol: str, candle: dict) -> Bar:
        return Bar(
            symbol=symbol,
            timestamp=ensure_utc(datetime.fromtimestamp(candle["datetime"] / 1000, tz=UTC)),
            open=float(candle["open"]),
            high=float(candle["high"]),
            low=float(candle["low"]),
            close=float(candle["close"]),
            volume=float(candle["volume"]),
        )

    @classmethod
    def _latest_completed_bar(cls, symbol: str, candles: list[dict], now: datetime) -> Bar | None:
        if not candles:
            return None
        minute_floor = ensure_utc(now).replace(second=0, microsecond=0)
        completed = [
            candle for candle in candles
            if ensure_utc(datetime.fromtimestamp(candle["datetime"] / 1000, tz=UTC)) < minute_floor
        ]
        if not completed:
            return None
        return cls._bar_from_candle(symbol, completed[-1])


def _first_float(*values: object) -> float | None:
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None


def _first_int(*values: object) -> int | None:
    for value in values:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None
