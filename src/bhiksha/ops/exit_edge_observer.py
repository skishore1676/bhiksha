"""Independent, quote-only owner for the existing Exit Edge recorder.

This process has no order client, order endpoint, or position mutation path.
The executor writes frozen registrations; this owner reads those registrations
and appends observed Public option quotes to the same isolated SQLite store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
import json
from pathlib import Path
import sqlite3
import time as clock
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from bhiksha.execution.brokers.public.auth import get_access_token
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.execution.quote_lineage import extract_public_quote_timestamp
from bhiksha.market_data.trading_calendar import is_trading_day
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository

CENTRAL = ZoneInfo("America/Chicago")
POLL_SECONDS = 15.0
MAX_BATCH = 20


class PublicOptionQuoteReader:
    """Fixed market-data routes only; deliberately exposes no order method."""

    def __init__(self, settings: PublicBrokerSettings | None = None) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self.client = httpx.AsyncClient(
            base_url=self.settings.public_api_base_url.rstrip("/"), timeout=25.0,
        )
        self._account_id: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await get_access_token(self.settings)}",
                "Content-Type": "application/json"}

    async def _account(self) -> str:
        if self._account_id:
            return self._account_id
        cache = Path(self.settings.session_file).with_name("public_account.json")
        if cache.is_file():
            value = json.loads(cache.read_text(encoding="utf-8")).get("accountId")
            if value:
                self._account_id = str(value)
                return self._account_id
        response = await self.client.get("/userapigateway/trading/account", headers=await self._headers())
        response.raise_for_status()
        accounts = response.json().get("accounts") or []
        if not accounts or not accounts[0].get("accountId"):
            raise ValueError("Public account identity unavailable for market data")
        self._account_id = str(accounts[0]["accountId"])
        return self._account_id

    async def quotes(self, symbols: tuple[str, ...]) -> dict[str, Any]:
        account = await self._account()
        result: dict[str, Any] = {}
        for offset in range(0, len(symbols), MAX_BATCH):
            batch = symbols[offset:offset + MAX_BATCH]
            response = await self.client.post(
                f"/userapigateway/marketdata/{account}/quotes",
                headers=await self._headers(),
                json={"instruments": [{"symbol": symbol, "type": "OPTION"} for symbol in batch]},
            )
            response.raise_for_status()
            for item in response.json().get("quotes") or []:
                raw_symbol = str((item.get("instrument") or {}).get("symbol") or "").upper().replace(" ", "")
                if raw_symbol not in batch:
                    continue
                timestamp, field, bid_timestamp, ask_timestamp = extract_public_quote_timestamp(item)
                result[raw_symbol] = SimpleNamespace(
                    quote_timestamp=timestamp,
                    quote_timestamp_field=field,
                    bid_timestamp=bid_timestamp,
                    ask_timestamp=ask_timestamp,
                    bid=item.get("bid"), ask=item.get("ask"), last=item.get("last"),
                )
        return result


def _regular_session(now: datetime) -> bool:
    local = now.astimezone(CENTRAL)
    return is_trading_day(local.date()) and time(8, 30) <= local.time() <= time(15, 0)


def recover_registration_intents(
    event_db_path: str | Path, edge_db_path: str | Path, after_id: int,
) -> int:
    """Idempotently deliver frozen fill intents missed by the executor queue."""
    source = Path(event_db_path).resolve()
    if not source.is_file():
        return after_id
    repository = ProspectiveQuoteTapeRepository(edge_db_path, write_timeout_seconds=0.25)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=0.25) as conn:
        for _ in range(5):  # bounded catch-up; quote polling resumes after this pass
            rows = conn.execute(
                "SELECT id,event_type,payload FROM events WHERE id>? AND event_type IN "
                "('exit_edge_registration_intent','signal_outcome','shadow_entry_modeled') "
                "ORDER BY id LIMIT 1000", (after_id,),
            ).fetchall()
            for event_id, event_type, raw in rows:
                item: dict[str, Any] = {}
                try:
                    event = json.loads(raw)
                    item = event if event_type == "exit_edge_registration_intent" else event.get("exit_edge_registration")
                    if item is not None:
                        attempt = dict(item["attempt"])
                        cohort = item.get("cohort")
                        if cohort is not None:
                            repository.register_cohort(cohort)
                            attempt.update(outcome="registered", reason=None)
                        if not repository.try_record_registration_attempt(attempt):
                            return after_id
                except sqlite3.OperationalError:
                    return after_id  # transient writer lock; retry this event next poll
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    # Bad frozen data is evidence of a failure; preserve it if
                    # an attempt identity exists, then continue to later fills.
                    attempt = item.get("attempt") if isinstance(item, dict) else None
                    if isinstance(attempt, dict):
                        attempt.update(outcome="registration_persistence_failure",
                                       reason=f"invalid_frozen_intent:{type(exc).__name__}:{exc}"[:240])
                        if not repository.try_record_registration_attempt(attempt):
                            return after_id
                after_id = int(event_id)
            if len(rows) < 1000:
                # Do not rescan the same irrelevant event tail every 15 seconds.
                return int(conn.execute("SELECT COALESCE(MAX(id),?) FROM events", (after_id,)).fetchone()[0])
    return after_id


async def run_observer(
    *,
    db_path: str | Path,
    event_db_path: str | Path | None = None,
    status_path: str | Path,
    enable_marker: str | Path,
    reader: Any | None = None,
    stop: asyncio.Event | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Keep one observation owner alive across executor lifecycle changes."""
    marker = Path(enable_marker)
    reader = reader or PublicOptionQuoteReader()
    stop = stop or asyncio.Event()
    recorder = ExitEdgeLiveRecorder(db_path=db_path, status_path=status_path, role="observer")
    recorder.start()
    last_intent_id = 0
    try:
        while not stop.is_set():
            if recorder.snapshot().get("ready") and marker.is_file():
                try:
                    if event_db_path is not None:
                        last_intent_id = recover_registration_intents(
                            event_db_path, db_path, last_intent_id,
                        )
                    recorder.refresh_active_from_store()
                    now = datetime.now(UTC)
                    recorder.censor_expired_options(now)
                    symbols = recorder.active_option_symbols()
                    if symbols and _regular_session(now):
                        started = clock.monotonic()
                        quotes = await reader.quotes(symbols)
                        received = datetime.now(UTC)
                        for symbol in symbols:
                            quote = quotes.get(symbol)
                            if quote is not None:
                                recorder.observe_quote(symbol, quote, received)
                        recorder.record_observation_poll(
                            len(symbols), len(quotes),
                            quote_request_ms=(clock.monotonic() - started) * 1000,
                        )
                    else:
                        recorder.heartbeat()
                except Exception as exc:
                    recorder.record_observation_error(type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(float(poll_seconds), 1.0))
            except TimeoutError:
                pass
    finally:
        recorder.close(join_timeout_seconds=5.0)
        await reader.close()
