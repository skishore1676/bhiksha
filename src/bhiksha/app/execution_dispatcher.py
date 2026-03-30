"""Per-symbol execution dispatch for broker-affecting work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger


@dataclass(slots=True, frozen=True)
class ExecutionTask:
    symbol: str
    key: str
    runner: Callable[[], Awaitable[None]]


class SymbolExecutionDispatcher:
    """Runs broker-affecting actions serially per symbol."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[ExecutionTask | None]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._pending_keys: dict[str, set[str]] = {}

    def start(self, symbols: list[str]) -> None:
        for symbol in symbols:
            if symbol in self._workers:
                continue
            queue: asyncio.Queue[ExecutionTask | None] = asyncio.Queue()
            self._queues[symbol] = queue
            self._pending_keys[symbol] = set()
            self._workers[symbol] = asyncio.create_task(self._worker(symbol, queue))

    async def stop(self) -> None:
        for symbol, queue in self._queues.items():
            await queue.put(None)
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

    def submit(
        self,
        symbol: str,
        *,
        key: str,
        runner: Callable[[], Awaitable[None]],
    ) -> bool:
        queue = self._queues[symbol]
        pending_keys = self._pending_keys[symbol]
        if key in pending_keys:
            return False
        pending_keys.add(key)
        queue.put_nowait(ExecutionTask(symbol=symbol, key=key, runner=runner))
        return True

    async def _worker(self, symbol: str, queue: asyncio.Queue[ExecutionTask | None]) -> None:
        while True:
            task = await queue.get()
            if task is None:
                return
            try:
                await task.runner()
            except Exception as exc:
                logger.exception("Execution task failed for {} key={}: {}", symbol, task.key, exc)
            finally:
                self._pending_keys[symbol].discard(task.key)
