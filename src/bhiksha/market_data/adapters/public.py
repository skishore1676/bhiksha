"""Public market-data adapter for underlying bars and live prices."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

from bhiksha.domain.models import Bar
from bhiksha.execution.brokers.public.account import get_primary_account_id
from bhiksha.execution.brokers.public.client import PublicApiClient
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.market_data.adapters.base import UnderlyingBarSource
from bhiksha.market_data.session import ensure_utc

PUBLIC_BARS_SESSIONS = ("regularMarket",)


class PublicBarSource(UnderlyingBarSource):
    """Warm-start and poll closed underlying bars via Public market data."""

    def __init__(
        self,
        client: PublicApiClient | None = None,
        settings: PublicBrokerSettings | None = None,
        *,
        poll_interval_seconds: int = 15,
        instrument_type: str = "EQUITY",
        aggregation: str = "ONE_MINUTE",
    ) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self.client = client or PublicApiClient(self.settings)
        self.poll_interval_seconds = poll_interval_seconds
        self.instrument_type = instrument_type
        self.aggregation = aggregation

    async def close(self) -> None:
        await self.client.close()

    async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        period = _period_for_window(start_utc, end_utc)
        payload = await self.client.get(
            f"/userapigateway/historicdata/{self.instrument_type}/{symbol}/{period}/{self.aggregation}"
        )
        bars = [
            bar
            for bar in self._parse_bars(symbol, payload)
            if start_utc <= bar.timestamp <= end_utc
        ]
        bars.sort(key=lambda bar: bar.timestamp)
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
        end = ensure_utc(now or datetime.now(UTC))
        bars = await self.warm_start(symbol, end - timedelta(minutes=5), end)
        minute_floor = end.replace(second=0, microsecond=0)
        completed = [bar for bar in bars if bar.timestamp < minute_floor]
        return completed[-1] if completed else None

    async def fetch_live_price(self, symbol: str) -> tuple[float, datetime] | None:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        payload = await self.client.post(
            f"/userapigateway/marketdata/{account_id}/quotes",
            json_data={"instruments": [{"symbol": symbol, "type": self.instrument_type}]},
        )
        quotes = payload.get("quotes", []) or []
        if not quotes:
            return None
        quote = quotes[0]
        price = _first_float(
            quote.get("last"),
            _midpoint(quote.get("bid"), quote.get("ask")),
            quote.get("previousClose"),
        )
        timestamp = _first_timestamp(
            quote.get("lastTimestamp"),
            quote.get("bidTimestamp"),
            quote.get("askTimestamp"),
        )
        if price is None or timestamp is None:
            return None
        return price, timestamp

    @staticmethod
    def _parse_bars(symbol: str, payload: dict[str, Any]) -> list[Bar]:
        bars: list[Bar] = []
        for session_name in PUBLIC_BARS_SESSIONS:
            session_payload = payload.get(session_name) or {}
            for raw in session_payload.get("bars") or []:
                bar = _bar_from_public_payload(symbol, raw)
                if bar is not None:
                    bars.append(bar)
        return bars


def _period_for_window(start: datetime, end: datetime) -> str:
    days = max((ensure_utc(end) - ensure_utc(start)).days, 0)
    if days <= 1:
        return "DAY"
    if days <= 7:
        return "WEEK"
    if days <= 31:
        return "MONTH"
    if days <= 93:
        return "QUARTER"
    if days <= 183:
        return "HALF_YEAR"
    if days <= 366:
        return "YEAR"
    return "FIVE_YEARS"


def _bar_from_public_payload(symbol: str, raw: dict[str, Any]) -> Bar | None:
    try:
        timestamp = _parse_public_timestamp(raw["timestamp"])
        return Bar(
            symbol=symbol,
            timestamp=timestamp,
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=float(raw.get("volume") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_public_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return ensure_utc(parsed)


def _first_float(*values: object) -> float | None:
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None


def _midpoint(bid: object, ask: object) -> float | None:
    bid_value = _first_float(bid)
    ask_value = _first_float(ask)
    if bid_value is None or ask_value is None:
        return None
    return (bid_value + ask_value) / 2


def _first_timestamp(*values: object) -> datetime | None:
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        try:
            return _parse_public_timestamp(value)
        except ValueError:
            continue
    return None
