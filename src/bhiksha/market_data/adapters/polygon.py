"""Polygon market-data adapter for historical warm starts."""

from __future__ import annotations

import asyncio
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

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.polygon.io",
        *,
        max_retries: int = 2,
        retry_delay_seconds: float = 65.0,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("POLYGON_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        if not self.api_key:
            raise ValueError("POLYGON_API_KEY is not set")

    async def close(self) -> None:
        return None

    async def warm_start(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        start_date = start.date()
        end_date = end.date()
        endpoint = f"/v2/aggs/ticker/{symbol}/range/1/minute/{start_date.isoformat()}/{end_date.isoformat()}"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            payload = await self._fetch_with_retry(client, endpoint)
        return self._parse_bars(symbol, payload).copy()

    async def _fetch_with_retry(self, client: httpx.AsyncClient, endpoint: str) -> dict[str, Any]:
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self.api_key,
        }
        for attempt in range(self.max_retries + 1):
            response = await client.get(endpoint, params=params)
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = _retry_after_seconds(response) or self.retry_delay_seconds
                await asyncio.sleep(retry_after)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Polygon warm-start failed status={response.status_code} endpoint={endpoint}"
                ) from exc
            return response.json()
        raise RuntimeError(f"Polygon warm-start exhausted retries endpoint={endpoint}")

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


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return max(parsed, 0.0)
