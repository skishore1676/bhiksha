"""Public broker adapter wired to the Bhiksha broker interface."""

from __future__ import annotations

from typing import Any

from bhiksha.execution.brokers.base import BrokerAdapter
from bhiksha.execution.brokers.public.account import get_accounts, get_portfolio, get_primary_account_id
from bhiksha.execution.brokers.public.client import PublicApiClient
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings


class PublicBrokerAdapter(BrokerAdapter):
    """Execution adapter for Public broker trading operations."""

    def __init__(self, settings: PublicBrokerSettings | None = None) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self.client = PublicApiClient(self.settings)

    async def close(self) -> None:
        await self.client.close()

    async def get_positions(self) -> list[dict[str, Any]]:
        portfolio = await get_portfolio(client=self.client, settings=self.settings)
        return portfolio.get("positions", []) or []

    async def get_portfolio(self) -> dict[str, Any]:
        return await get_portfolio(client=self.client, settings=self.settings)

    async def get_account_info(self) -> dict[str, Any]:
        accounts = await get_accounts(client=self.client, settings=self.settings)
        if not accounts:
            raise ValueError("No Public trading accounts returned by API")
        return accounts[0]

    async def get_orders(self) -> list[dict[str, Any]]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        history = await self.client.get(f"/userapigateway/trading/{account_id}/history")
        return history.get("transactions", []) or []

    async def get_order(self, order_id: str) -> dict[str, Any]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        return await self.client.get(f"/userapigateway/trading/{account_id}/order/{order_id}")

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        return await self.client.delete(f"/userapigateway/trading/{account_id}/order/{order_id}")

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        return await self.client.post(f"/userapigateway/trading/{account_id}/order", json_data=order)

    async def preflight_single_leg(self, order: dict[str, Any]) -> dict[str, Any]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        return await self.client.post(f"/userapigateway/trading/{account_id}/preflight/single-leg", json_data=order)

    async def get_quotes(self, instruments: list[dict[str, str]]) -> dict[str, Any]:
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        return await self.client.post(
            f"/userapigateway/marketdata/{account_id}/quotes",
            json_data={"instruments": instruments},
        )
