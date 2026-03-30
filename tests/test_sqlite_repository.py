import asyncio
import sqlite3

from bhiksha.persistence.sqlite import SQLiteEventRepository


def test_sqlite_event_repository_appends_rows(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    asyncio.run(repo.append("test_event", {"hello": "world"}))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT event_type, payload FROM events").fetchone()
    assert row[0] == "test_event"
    assert "hello" in row[1]

