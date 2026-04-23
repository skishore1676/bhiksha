"""Shared async retry helpers for transient network/provider failures."""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

import httpx

ResultT = TypeVar("ResultT")


async def retry_async(
    operation: Callable[[], Awaitable[ResultT]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 8.0,
    jitter_seconds: float = 0.2,
    is_retryable: Callable[[Exception], bool] | None = None,
    on_retry: Callable[[Exception, int, float], Awaitable[None]] | None = None,
) -> ResultT:
    if attempts <= 1:
        return await operation()
    predicate = is_retryable or is_retryable_http_error
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not predicate(exc):
                raise
            delay = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
            delay += random.uniform(0.0, jitter_seconds)
            if on_retry is not None:
                await on_retry(exc, attempt, delay)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False
