"""Account and portfolio helpers for the Public broker."""

from __future__ import annotations

import json
from pathlib import Path

from bhiksha.execution.brokers.public.client import PublicApiClient
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings


def _read_cached_account_id(cache_file: str | Path) -> str | None:
    path = Path(cache_file)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("accountId")
    except Exception:
        return None


def _write_cached_account_id(cache_file: str | Path, account_id: str) -> None:
    path = Path(cache_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"accountId": account_id}, indent=2), encoding="utf-8")


async def get_primary_account_id(
    client: PublicApiClient | None = None,
    settings: PublicBrokerSettings | None = None,
) -> str:
    """Return the first available Public trading account ID."""
    settings = settings or PublicBrokerSettings.from_env()
    cache_file = Path(settings.session_file).with_name("public_account.json")
    cached = _read_cached_account_id(cache_file)
    if cached:
        return cached

    owns_client = client is None
    client = client or PublicApiClient(settings)
    try:
        payload = await client.get("/userapigateway/trading/account")
        accounts = payload.get("accounts", [])
        if not accounts:
            raise ValueError("No Public trading accounts returned by API")
        account_id = accounts[0].get("accountId")
        if not account_id:
            raise ValueError("Primary Public account ID missing from API response")
        _write_cached_account_id(cache_file, account_id)
        return str(account_id)
    finally:
        if owns_client:
            await client.close()


async def get_accounts(
    client: PublicApiClient | None = None,
    settings: PublicBrokerSettings | None = None,
) -> list[dict]:
    """Fetch all available Public trading accounts."""
    settings = settings or PublicBrokerSettings.from_env()
    owns_client = client is None
    client = client or PublicApiClient(settings)
    try:
        payload = await client.get("/userapigateway/trading/account")
        return payload.get("accounts", []) or []
    finally:
        if owns_client:
            await client.close()


async def get_portfolio(
    client: PublicApiClient | None = None,
    settings: PublicBrokerSettings | None = None,
) -> dict:
    """Fetch the current Public portfolio payload."""
    settings = settings or PublicBrokerSettings.from_env()
    owns_client = client is None
    client = client or PublicApiClient(settings)
    try:
        account_id = await get_primary_account_id(client=client, settings=settings)
        return await client.get(f"/userapigateway/trading/{account_id}/portfolio/v2")
    finally:
        if owns_client:
            await client.close()
