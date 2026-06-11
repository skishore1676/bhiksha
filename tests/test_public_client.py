"""Tests for Public API client error handling."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from loguru import logger

from bhiksha.execution.brokers.public.client import PublicApiClient


def _response(status_code: int, body: str) -> httpx.Response:
    request = httpx.Request("POST", "https://api.public.com/userapigateway/trading/ACCT/order")
    return httpx.Response(status_code=status_code, request=request, text=body)


def test_response_body_excerpt_truncates_and_handles_empty() -> None:
    assert PublicApiClient._response_body_excerpt(_response(400, "")) == "<empty>"
    assert PublicApiClient._response_body_excerpt(_response(400, "  short  ")) == "short"
    long_body = "x" * 600
    excerpt = PublicApiClient._response_body_excerpt(_response(400, long_body))
    assert len(excerpt) == 500


def test_handle_response_logs_error_body_on_rejection() -> None:
    client = PublicApiClient.__new__(PublicApiClient)
    response = _response(400, '{"message": "stop price below minimum tick"}')
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(PublicApiClient._handle_response(client, "POST", "/order", response))
    finally:
        logger.remove(sink_id)

    assert any("stop price below minimum tick" in line for line in captured)
    assert any("status 400" in line for line in captured)
