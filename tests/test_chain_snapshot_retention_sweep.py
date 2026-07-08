"""Tests for the chain-snapshot startup retention sweep in runtime.py."""

import asyncio
from datetime import UTC, datetime, timedelta

from bhiksha.app.runtime import _CHAIN_SNAPSHOT_RETENTION_DAYS, _sweep_chain_snapshot_retention_best_effort


class SpyChainSnapshotRepository:
    def __init__(self, *, deleted: int = 0) -> None:
        self.deleted = deleted
        self.purge_calls: list = []

    async def purge_older_than(self, cutoff) -> int:
        self.purge_calls.append(cutoff)
        return self.deleted


class RaisingChainSnapshotRepository:
    async def purge_older_than(self, cutoff) -> int:
        del cutoff
        raise RuntimeError("db locked")


def test_sweep_uses_configured_retention_window() -> None:
    repository = SpyChainSnapshotRepository(deleted=5)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    asyncio.run(_sweep_chain_snapshot_retention_best_effort(repository, now=now))

    assert len(repository.purge_calls) == 1
    expected_cutoff = now - timedelta(days=_CHAIN_SNAPSHOT_RETENTION_DAYS)
    assert repository.purge_calls[0] == expected_cutoff


def test_sweep_logs_when_rows_deleted() -> None:
    repository = SpyChainSnapshotRepository(deleted=12)
    messages: list[str] = []

    asyncio.run(_sweep_chain_snapshot_retention_best_effort(repository, output=messages.append))

    assert any("CHAIN_SNAPSHOT_RETENTION_SWEEP" in message and "deleted=12" in message for message in messages)


def test_sweep_never_raises_on_repository_failure() -> None:
    messages: list[str] = []

    # Must not raise -- a startup sweep failure should never block the
    # trading loop from starting.
    asyncio.run(_sweep_chain_snapshot_retention_best_effort(RaisingChainSnapshotRepository(), output=messages.append))

    assert any("CHAIN_SNAPSHOT_RETENTION_SWEEP_FAILED" in message for message in messages)
