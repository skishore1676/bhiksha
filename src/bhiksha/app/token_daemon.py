"""Background token refresh daemons."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from loguru import logger

from bhiksha.execution.brokers.public.auth import refresh_and_store_access_token
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.execution.brokers.public.token_store import read_session
from bhiksha.integrations.schwab.auth import refresh_access_token, refresh_token_is_expired
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens


class PublicTokenRefreshDaemon:
    def __init__(
        self,
        settings: PublicBrokerSettings | None = None,
        *,
        buffer_seconds: int = 300,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self.buffer_seconds = buffer_seconds
        self._sleep = sleep
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        while not self._stopped:
            delay = self.next_refresh_delay_seconds(self.settings, buffer_seconds=self.buffer_seconds)
            await self._sleep(max(delay, 0.0))
            if self._stopped:
                return
            try:
                await refresh_and_store_access_token(self.settings)
            except Exception as exc:
                logger.warning("Public token daemon refresh failed: {}", exc)
                await self._sleep(30)

    @staticmethod
    def next_refresh_delay_seconds(settings: PublicBrokerSettings, *, buffer_seconds: int = 300) -> float:
        session = read_session(settings.session_file)
        if not session:
            return 0.0
        expiration_timestamp = float(session.get("expiration_timestamp") or 0.0)
        return max(expiration_timestamp - time.time() - buffer_seconds, 0.0)


class SchwabTokenRefreshDaemon:
    def __init__(
        self,
        settings: SchwabSettings | None = None,
        *,
        buffer_seconds: int = 300,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or SchwabSettings.from_env()
        self.buffer_seconds = buffer_seconds
        self._sleep = sleep
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        while not self._stopped:
            delay = self.next_refresh_delay_seconds(self.settings, buffer_seconds=self.buffer_seconds)
            await self._sleep(max(delay, 0.0))
            if self._stopped:
                return
            try:
                await refresh_access_token(self.settings)
            except Exception as exc:
                logger.warning("Schwab token daemon refresh failed: {}", exc)
                await self._sleep(30)

    @staticmethod
    def next_refresh_delay_seconds(settings: SchwabSettings, *, buffer_seconds: int = 300) -> float:
        payload = read_tokens(settings.token_file)
        if not payload:
            return 0.0
        if refresh_token_is_expired(payload):
            return 0.0
        issued_at = datetime.fromisoformat(payload["access_token_issued"]).astimezone(UTC)
        refresh_at = issued_at + timedelta(seconds=1800 - buffer_seconds)
        return max((refresh_at - datetime.now(UTC)).total_seconds(), 0.0)
