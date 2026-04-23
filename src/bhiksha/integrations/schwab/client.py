"""Minimal Schwab REST client for market data and account discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from bhiksha.integrations.schwab.auth import get_valid_access_token
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.net.retry import retry_async


class SchwabApiClient:
    """REST client for the Schwab API using stored OAuth tokens."""

    def __init__(self, settings: SchwabSettings | None = None) -> None:
        self.settings = settings or SchwabSettings.from_env()
        self._client = httpx.AsyncClient(base_url=self.settings.api_base_url.rstrip("/"), timeout=self.settings.timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await get_valid_access_token(self.settings)}"}

    async def linked_accounts(self) -> list[dict[str, Any]]:
        async def operation() -> list[dict[str, Any]]:
            response = await self._client.get("/trader/v1/accounts/accountNumbers", headers=await self._headers())
            response.raise_for_status()
            return response.json()

        return await retry_async(operation)

    async def quote(self, symbol: str) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            response = await self._client.get(f"/marketdata/v1/{symbol}/quotes", headers=await self._headers())
            response.raise_for_status()
            return response.json()

        return await retry_async(operation)

    async def option_chain(
        self,
        symbol: str,
        *,
        contract_type: str = "ALL",
        strike_count: int = 20,
        from_date: datetime.date | None = None,
        to_date: datetime.date | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "contractType": contract_type,
            "strikeCount": strike_count,
            "includeUnderlyingQuote": "false",
        }
        if from_date is not None:
            params["fromDate"] = from_date.isoformat()
        if to_date is not None:
            params["toDate"] = to_date.isoformat()
        async def operation() -> dict[str, Any]:
            response = await self._client.get("/marketdata/v1/chains", headers=await self._headers(), params=params)
            response.raise_for_status()
            return response.json()

        return await retry_async(operation)

    async def price_history(
        self,
        symbol: str,
        *,
        period_type: str = "day",
        period: int = 1,
        frequency_type: str = "minute",
        frequency: int = 1,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
        }
        if start_date is not None:
            params["startDate"] = int(start_date.timestamp() * 1000)
        if end_date is not None:
            params["endDate"] = int(end_date.timestamp() * 1000)
        async def operation() -> dict[str, Any]:
            response = await self._client.get(
                f"/marketdata/v1/pricehistory",
                headers=await self._headers(),
                params={"symbol": symbol, **params},
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(operation)
