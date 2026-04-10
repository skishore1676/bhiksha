"""Polygon market-data adapter for historical warm starts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import os
from typing import AsyncIterator
from typing import Any

import httpx

from bhiksha.config.environment import load_dotenv
from bhiksha.domain.models import Bar
from bhiksha.market_data.adapters.base import UnderlyingBarSource


class PolygonBarSource(UnderlyingBarSource):
    """Warm-start bar loader using Polygon aggregates."""

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.polygon.io") -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("POLYGON_API_KEY")
        self.base_url = base_url.rstrip("/")
        if not self.api_key:
            raise ValueError("POLYGON_API_KEY is not set")

    async def close(self) -> None:
        return None

    async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        start_date = start.date()
        end_date = end.date()
        endpoint = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start_date.isoformat()}/{end_date.isoformat()}"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.get(
                endpoint,
                params={
                    "adjusted": "true",
                    "sort": "asc",
                    "limit": 50000,
                    "apiKey": self.api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        return self._parse_bars(symbol, payload).copy()

    async def stream_closed_bars(self, symbols: list[str]) -> AsyncIterator[Bar]:
        raise NotImplementedError("Polygon is only used for warm-start data in Day 1")

    async def fetch_latest_completed_bar(self, symbol: str, *, now: datetime | None = None) -> Bar | None:
        end = now or datetime.now(UTC)
        bars = await self.warm_start(symbol, end - timedelta(minutes=5), end)
        minute_floor = end.replace(second=0, microsecond=0)
        completed = [bar for bar in bars if bar.timestamp < minute_floor]
        return completed[-1] if completed else None

    async def fetch_live_price(self, symbol: str) -> tuple[float, datetime] | None:
        return None

    @staticmethod
    def _parse_bars(symbol: str, payload: dict[str, Any]) -> list[Bar]:
        bars: list[Bar] = []
        for result in payload.get("results", []):
            timestamp = datetime.fromtimestamp(result["t"] / 1000, tz=UTC)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=float(result["o"]),
                    high=float(result["h"]),
                    low=float(result["l"]),
                    close=float(result["c"]),
                    volume=float(result["v"]),
                )
            )
        return bars
