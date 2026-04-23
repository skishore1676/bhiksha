"""Thin Public broker API client."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from bhiksha.execution.brokers.public.auth import get_access_token
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.net.retry import retry_async


class RateLimiter:
    """Token-bucket rate limiter with light 429 backoff."""

    def __init__(self, requests_per_second: float = 5.0, burst_limit: int = 10) -> None:
        self.requests_per_second = requests_per_second
        self.burst_limit = burst_limit
        self.tokens = burst_limit
        self.last_update = time.time()
        self.lock = asyncio.Lock()
        self.backoff_multiplier = 1.0
        self.max_backoff = 60.0
        self.base_backoff = 1.0

    async def acquire(self) -> None:
        async with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
            self.last_update = now
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.requests_per_second
                await asyncio.sleep(wait_time)
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
                self.last_update = now
            if self.tokens < 1.0:
                raise RuntimeError("Rate limiter could not acquire a token")
            self.tokens -= 1.0

    async def handle_429_response(self) -> None:
        async with self.lock:
            backoff_time = min(self.base_backoff * self.backoff_multiplier, self.max_backoff)
            logger.warning("Public API 429 encountered; backing off for {:.1f}s", backoff_time)
            await asyncio.sleep(backoff_time)
            self.backoff_multiplier = min(self.backoff_multiplier * 2.0, 8.0)

    def reset_backoff(self) -> None:
        self.backoff_multiplier = 1.0


class PublicApiClient:
    """Async client for authenticated Public API requests."""

    def __init__(self, settings: PublicBrokerSettings | None = None) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self._client = httpx.AsyncClient(
            base_url=self.settings.public_api_base_url.rstrip("/"),
            timeout=30.0,
        )
        self._rate_limiter = RateLimiter(
            requests_per_second=self.settings.api_requests_per_second,
            burst_limit=self.settings.api_burst_limit,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await get_access_token(self.settings)}",
            "Content-Type": "application/json",
        }

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            await self._rate_limiter.acquire()
            response = await self._client.get(endpoint, headers=await self._headers(), params=params)
            return await self._handle_response("GET", endpoint, response)

        return await retry_async(operation, on_retry=self._log_retry)

    async def post(self, endpoint: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            await self._rate_limiter.acquire()
            response = await self._client.post(endpoint, headers=await self._headers(), json=json_data)
            return await self._handle_response("POST", endpoint, response)

        return await retry_async(operation, on_retry=self._log_retry)

    async def delete(self, endpoint: str) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            await self._rate_limiter.acquire()
            response = await self._client.delete(endpoint, headers=await self._headers())
            return await self._handle_response("DELETE", endpoint, response)

        return await retry_async(operation, on_retry=self._log_retry)

    async def _handle_response(
        self,
        method: str,
        endpoint: str,
        response: httpx.Response,
    ) -> dict[str, Any]:
        if response.status_code == 429:
            await self._rate_limiter.handle_429_response()
            response.raise_for_status()
        try:
            response.raise_for_status()
            self._rate_limiter.reset_backoff()
            if response.status_code == 204 or not response.text:
                return {}
            return response.json()
        except httpx.HTTPStatusError:
            logger.error("Public API {} {} failed with status {}", method, endpoint, response.status_code)
            raise

    async def _log_retry(self, exc: Exception, attempt: int, delay: float) -> None:
        logger.warning(
            "PUBLIC_API_RETRY attempt={} delay_s={:.2f} error_type={} error={}",
            attempt,
            delay,
            type(exc).__name__,
            exc,
        )
