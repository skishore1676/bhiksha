import asyncio
from contextlib import closing
import os
import sqlite3
from datetime import UTC, datetime

import pytest

from bhiksha.domain.models import TradeRecord
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository


def test_sqlite_event_repository_appends_rows(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    asyncio.run(repo.append("test_event", {"hello": "world"}))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT event_type, payload FROM events").fetchone()
    assert row[0] == "test_event"
    assert "hello" in row[1]


def test_sqlite_backend_enables_wal_and_busy_timeout(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    backend = SQLiteBackend(str(db_path))
    repo = SQLiteEventRepository(str(db_path), backend=backend)

    asyncio.run(repo.append("test_event", {"hello": "world"}))

    with closing(backend.connect()) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000


def test_sqlite_backend_serializes_shared_runtime_writes(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    backend = SQLiteBackend(str(db_path))
    event_repo = SQLiteEventRepository(str(db_path), backend=backend)
    trade_repo = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def run() -> None:
        await asyncio.gather(
            *(event_repo.append("runtime_metric", {"index": index}) for index in range(25)),
            *(
                trade_repo.upsert_trade(
                    TradeRecord(
                        trade_id=f"TRADE-{index}",
                        deployment_id="market_impulse_qqq_short_v1",
                        symbol="QQQ",
                        option_symbol=f"QQQ260401P00556{index:03d}",
                        quantity=1,
                        entry_price=2.5,
                        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                        status="open_protected",
                    )
                )
                for index in range(25)
            ),
        )

    asyncio.run(run())

    with sqlite3.connect(db_path) as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        trade_count = conn.execute("SELECT COUNT(*) FROM trade_sessions").fetchone()[0]
        statuses = {
            row[0]
            for row in conn.execute("SELECT status FROM trade_sessions").fetchall()
        }

    assert event_count == 25
    assert trade_count == 25
    assert statuses == {"open_protected"}


def test_trade_state_mark_closed_persists_exit_fill_truth(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    repo = SQLiteTradeStateRepository(str(db_path))
    filled_at = datetime(2026, 4, 30, 15, 2, 7, tzinfo=UTC)

    async def run() -> None:
        await repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE123",
                deployment_id="market_impulse_qqq_short_v1",
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                quantity=1,
                entry_price=2.5,
                entry_timestamp=datetime(2026, 4, 30, 14, 30, tzinfo=UTC),
                status="exit_pending",
                exit_order_id="EXIT123",
            )
        )
        await repo.mark_closed(
            "TRADE123",
            exit_order_id="EXIT123",
            exit_price=2.35,
            exit_filled_quantity=1,
            exit_filled_at=filled_at,
            exit_order_status="FILLED",
            exit_order_type="LIMIT",
            exit_broker_payload={"orderId": "EXIT123", "averagePrice": "2.35"},
        )

    asyncio.run(run())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at,
                   exit_order_status, exit_order_type, exit_broker_payload
            FROM trade_sessions
            WHERE trade_id = 'TRADE123'
            """
        ).fetchone()

    assert row[0] == "closed"
    assert row[1] == "EXIT123"
    assert row[2] == 2.35
    assert row[3] == 1
    assert row[4] == filled_at.isoformat()
    assert row[5] == "FILLED"
    assert row[6] == "LIMIT"
    assert "averagePrice" in row[7]


def test_trade_upsert_does_not_regress_pending_exit_to_stale_open_snapshot(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    repo = SQLiteTradeStateRepository(str(db_path))
    submitted_at = datetime(2026, 7, 13, 14, 38, 5, tzinfo=UTC)

    async def run() -> None:
        await repo.upsert_trade(
            TradeRecord(
                trade_id="NVDA-RACE",
                deployment_id="nvda_live",
                symbol="NVDA",
                option_symbol="NVDA260722P00200000",
                quantity=8,
                entry_price=2.18,
                status="exit_pending",
                entry_order_id="ENTRY-NVDA-RACE",
                exit_order_id="EXIT-NVDA-RACE",
                exit_limit_price=2.07,
                exit_submitted_at=submitted_at,
            )
        )
        # Mirrors the stale reconciliation upsert that landed after the exit.
        await repo.upsert_trade(
            TradeRecord(
                trade_id="NVDA-RACE",
                deployment_id="nvda_live",
                symbol="NVDA",
                option_symbol="NVDA260722P00200000",
                quantity=8,
                entry_price=2.18,
                status="open_protected",
                entry_order_id="ENTRY-NVDA-RACE",
                stop_order_id="STOP-NVDA-RACE",
            )
        )

    asyncio.run(run())

    record = asyncio.run(repo.get_recent_trades(limit=1))[0]
    assert record.status == "exit_pending"
    assert record.exit_order_id == "EXIT-NVDA-RACE"
    assert record.exit_limit_price == 2.07
    assert record.exit_submitted_at == submitted_at


def test_sqlite_event_repository_does_not_leak_file_descriptors(tmp_path) -> None:
    before = _open_fd_count()
    if before is None:
        pytest.skip("file descriptor count is not available on this platform")

    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    async def run() -> None:
        for index in range(100):
            await repo.append("runtime_metric", {"index": index})

    asyncio.run(run())

    after = _open_fd_count()
    assert after is not None
    assert after <= before + 3


def _open_fd_count() -> int | None:
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(fd_dir))
        except OSError:
            continue
    return None
