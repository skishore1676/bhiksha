"""Auth token refresh for the Public broker."""

from __future__ import annotations

import asyncio
import time

import httpx
from loguru import logger

from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.execution.brokers.public.token_store import read_session, write_session


async def refresh_and_store_access_token(settings: PublicBrokerSettings) -> str:
    """Refresh an access token using the configured personal secret token."""
    settings.validate_credentials()
    payload = {"secret": settings.public_secret_token, "validityInMinutes": 60}

    async with httpx.AsyncClient() as client:
        await asyncio.sleep(0.2)
        response = await client.post(settings.public_auth_endpoint, json=payload)
        response.raise_for_status()
        data = response.json()

    token = data.get("accessToken")
    expires_in = data.get("expiresIn") or 3600
    if not token:
        raise ValueError("Access token not found in Public auth response")

    expiration_time = int(time.time()) + int(expires_in)
    write_session(
        settings.session_file,
        {"access_token": token, "expiration_timestamp": expiration_time},
    )
    logger.info("Public access token refreshed; expires at {}", time.ctime(expiration_time))
    return token


async def get_access_token(settings: PublicBrokerSettings) -> str:
    """Return a valid cached Public access token, refreshing if necessary."""
    session = read_session(settings.session_file)
    if not session:
        return await refresh_and_store_access_token(settings)

    current_time = time.time()
    expiration_time = session.get("expiration_timestamp", 0)
    if expiration_time - current_time <= 300:
        return await refresh_and_store_access_token(settings)
    return str(session["access_token"])

