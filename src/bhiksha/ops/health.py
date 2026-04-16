"""Live-safe provider connectivity checks."""

from __future__ import annotations

import os

import httpx

from bhiksha.config.environment import load_dotenv
from bhiksha.execution.brokers.public.account import get_primary_account_id
from bhiksha.execution.brokers.public.client import PublicApiClient
from bhiksha.execution.brokers.public.auth import get_access_token
from bhiksha.execution.brokers.public.account import get_accounts, get_portfolio
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.integrations.schwab.auth import build_authorize_url, refresh_token_is_expired
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens


async def check_public_auth() -> tuple[bool, str]:
    """Validate Public auth and account access without placing any orders."""
    try:
        settings = PublicBrokerSettings.from_env()
        token = await get_access_token(settings)
        client = PublicApiClient(settings)
        try:
            account_id = await get_primary_account_id(client=client, settings=settings)
            accounts = await get_accounts(client=client, settings=settings)
            portfolio = await get_portfolio(client=client, settings=settings)
        finally:
            await client.close()
        account = accounts[0] if accounts else {}
        options_level = account.get("optionsLevel", "unknown")
        buying_power = (portfolio.get("buyingPower") or {}).get("optionsBuyingPower", "unknown")
        return True, (
            f"token_ok account_id={account_id} "
            f"options_level={options_level} "
            f"options_buying_power={buying_power}"
            if token else "token_missing"
        )
    except Exception as exc:
        return False, str(exc)


async def check_polygon() -> tuple[bool, str]:
    """Validate Polygon data access with a tiny aggregate request."""
    try:
        load_dotenv()
        api_key = os.getenv("POLYGON_API_KEY")
        if not api_key:
            return False, "POLYGON_API_KEY missing"
        response = httpx.get(
            "https://api.polygon.io/v2/aggs/ticker/QQQ/range/1/minute/2026-03-27/2026-03-27",
            params={"adjusted": "true", "sort": "asc", "limit": 1, "apiKey": api_key},
            timeout=20.0,
        )
        response.raise_for_status()
        count = len(response.json().get("results", []))
        return True, f"results={count}"
    except Exception as exc:
        return False, str(exc)


async def check_schwab_setup() -> tuple[bool, str]:
    """Validate Schwab env config without touching account or order endpoints."""
    try:
        settings = SchwabSettings.from_env()
        settings.validate_credentials()
        if settings.callback_needs_attention:
            return False, "callback_pending_approval:https://127.0.0.1:8182/callback"
        url = build_authorize_url(settings)
        return True, f"authorize_url_ready:{url}"
    except Exception as exc:
        return False, str(exc)


async def check_schwab_token_health() -> tuple[bool, str]:
    """Check if Schwab refresh token is valid (not expired). Long-term health safeguard."""
    try:
        settings = SchwabSettings.from_env()
        tokens = read_tokens(settings.token_file)
        if not tokens:
            return False, "token_file_missing"
        if refresh_token_is_expired(tokens):
            return False, "refresh_token_expired"
        return True, "token_valid"
    except Exception as exc:
        return False, str(exc)
