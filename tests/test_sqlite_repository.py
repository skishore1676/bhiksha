import asyncio
import sqlite3
from datetime import UTC, datetime

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

    with backend.connect() as conn:
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
