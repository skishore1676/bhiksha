"""Independent, quote-only owner for the existing Exit Edge recorder.

This process has no order client, order endpoint, or position mutation path.
The executor writes frozen registrations; this owner reads those registrations
and appends observed Public option quotes to the same isolated SQLite store.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
import os
import sys
from threading import Event, Thread
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
from bhiksha.market_data.trading_calendar import regular_session_bounds
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository

CENTRAL = ZoneInfo("America/Chicago")
POLL_SECONDS = 15.0
MAX_BATCH = 20
QUOTE_DEADLINE_SECONDS = 45.0
PROGRESS_DEADLINE_SECONDS = 180.0
IDLE_SECONDS = 60.0  # Lightweight status only; below the watchdog/freshness deadlines.
WARMUP = timedelta(minutes=10)


class ObserverProgressWatchdog:
    """Exit only this quote-only process on stalled progress; launchd owns restart."""

    def __init__(self, status_path, *, deadline=PROGRESS_DEADLINE_SECONDS,
                 monotonic=clock.monotonic, terminate=os._exit, writer_health=None):
        self.writer_health = writer_health
        self.status_path = Path(status_path)
        self.deadline = deadline
        self.monotonic = monotonic
        self.terminate = terminate
        self.progress = (monotonic(), 'starting')
        self.stopped = Event()
        self.thread = Thread(target=self._run, name='exit-observer-watchdog', daemon=True)

    def advance(self, stage):
        self.progress = (self.monotonic(), stage)

    def check(self):
        at, stage = self.progress
        writer = self.writer_health() if self.writer_health else {}
        writer_stalled = writer.get('oldest_pending_write_seconds', 0) > self.deadline
        if self.monotonic() - at <= self.deadline and not writer_stalled:
            return False
        if writer_stalled:
            stage = 'persist_queued_facts'
        failure = {'status': 'stalled', 'stage': stage,
                   'detected_at': datetime.now(UTC).isoformat(),
                   'reason': 'observer_progress_deadline_exceeded'}
        try:
            path = self.status_path.with_suffix('.watchdog.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(failure) + '\n')
            temp.replace(path)
            print(json.dumps(failure), file=sys.stderr, flush=True)
        finally:
            # No broker/order client exists in this owner. Avoid a hung cleanup
            # hiding failure from launchd's existing KeepAlive restart policy.
            self.terminate(1)
        return True

    def _run(self):
        while not self.stopped.wait(5):
            if self.check():
                return

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)



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
    bounds = regular_session_bounds(now.astimezone(CENTRAL).date())
    return bounds is not None and bounds[0] <= now < bounds[1]


def _session_phase(now: datetime) -> str:
    bounds = regular_session_bounds(now.astimezone(CENTRAL).date())
    if bounds is None or now < bounds[0] - WARMUP or now >= bounds[1]:
        return "closed"
    return "open" if now >= bounds[0] else "warmup"


def _wait_seconds(now: datetime, phase: str, poll_seconds: float) -> float:
    delay = IDLE_SECONDS if phase == "closed" else max(float(poll_seconds), 1.0)
    bounds = regular_session_bounds(now.astimezone(CENTRAL).date())
    if bounds:
        # Wake at warmup, open and close instead of overshooting a boundary.
        for boundary in (bounds[0] - WARMUP, bounds[0], bounds[1]):
            if boundary > now:
                delay = min(delay, (boundary - now).total_seconds())
                break
    return delay


def recover_registration_intents(
    event_db_path: str | Path, edge_db_path: str | Path, after_id: int,
) -> int:
    """Idempotently deliver frozen fill intents missed by the executor queue."""
    source = Path(event_db_path).resolve()
    if not source.is_file():
        return after_id
    repository = ProspectiveQuoteTapeRepository(edge_db_path, write_timeout_seconds=0.25)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=0.25)) as conn:
        # Freeze a ceiling before scanning, so concurrent inserts remain for the next poll.
        ceiling = int(conn.execute("SELECT COALESCE(MAX(id),?) FROM events", (after_id,)).fetchone()[0])
        for _ in range(5):  # bounded catch-up; quote polling resumes after this pass
            rows = conn.execute(
                "SELECT id,event_type,payload FROM events WHERE id>? AND id<=? AND event_type IN "
                "('exit_edge_registration_intent','signal_outcome','shadow_entry_modeled') "
                "ORDER BY id LIMIT 1000", (after_id, ceiling),
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
                return ceiling
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
    now_fn: Any = None,
) -> None:
    """Keep one observation owner alive across executor lifecycle changes."""
    marker = Path(enable_marker)
    now_fn = now_fn or (lambda: datetime.now(UTC))
    stop = stop or asyncio.Event()
    recorder = ExitEdgeLiveRecorder(db_path=db_path, status_path=status_path, role="observer")
    watchdog = ObserverProgressWatchdog(status_path, writer_health=recorder.snapshot)
    watchdog.thread.start()
    started_worker = False
    last_intent_id = 0
    try:
        while not stop.is_set():
            now = now_fn()
            phase = _session_phase(now)
            enabled = marker.is_file()
            health = recorder.snapshot()
            if started_worker and health.get("ready") and not health.get("worker_alive"):
                raise RuntimeError("exit_observer_writer_stopped")
            if phase == "closed" or not enabled:
                # A cold off-hours start does not even initialize/replay SQLite.
                # A warm owner retains unfinished comparisons in memory; its writer
                # finishes already-queued facts without a new scan or quote call.
                draining = any(health.get(key, 0) for key in
                               ("pending_writes", "pending_censors", "pending_registration_attempts"))
                mode = "draining_market_close" if draining else "idle_market_closed" if enabled else "disabled"
                recorder.heartbeat(mode=mode)
                watchdog.advance(mode)
            else:
                if not started_worker:
                    recorder.start()
                    started_worker = True
                    watchdog.advance('recover_saved_comparisons')
                # Do not reset progress while recovery is still running:
                # the existing watchdog bounds failed or stalled startup too.
            if phase != "closed" and enabled and recorder.snapshot().get("ready"):
                try:
                    watchdog.advance('recover_registration_intents')
                    if event_db_path is not None:
                        last_intent_id = recover_registration_intents(
                            event_db_path, db_path, last_intent_id,
                        )
                    watchdog.advance('refresh_cohorts')
                    recorder.refresh_active_from_store()
                    now = now_fn()
                    recorder.censor_expired_options(now)
                    symbols = recorder.active_option_symbols()
                    if symbols and _regular_session(now):
                        reader = reader or PublicOptionQuoteReader()
                        started = clock.monotonic()
                        watchdog.advance('request_quotes')
                        async with asyncio.timeout(QUOTE_DEADLINE_SECONDS):
                            quotes = await reader.quotes(symbols)
                        watchdog.advance('persist_quotes')
                        received = now_fn()
                        for symbol in symbols:
                            quote = quotes.get(symbol)
                            if quote is not None:
                                recorder.observe_quote(symbol, quote, received)
                        recorder.record_observation_poll(
                            len(symbols), len(quotes),
                            quote_request_ms=(clock.monotonic() - started) * 1000,
                        )
                    else:
                        recorder.heartbeat(mode="warming_market_open" if phase == "warmup" else "idle_no_cohorts")
                except Exception as exc:
                    recorder.record_observation_error(type(exc).__name__)
                watchdog.advance('poll_complete')
            try:
                wait_now = now_fn()
                wait_phase = _session_phase(wait_now)
                delay = _wait_seconds(wait_now, wait_phase, poll_seconds)
                if wait_phase != phase:
                    delay = 0.0  # Publish/drain immediately if a request crossed the close.
                if phase == "closed" and draining:
                    delay = min(delay, 1.0)
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        watchdog.advance('shutdown')
        recorder.close(join_timeout_seconds=5.0)
        if reader is not None:
            await reader.close()
        watchdog.close()
