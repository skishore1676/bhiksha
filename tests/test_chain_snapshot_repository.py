import asyncio
from contextlib import closing
import sqlite3
from datetime import UTC, datetime, timedelta

from bhiksha.options.chain_snapshot import ChainSnapshotAttempt, ContractSnapshotRow
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteChainSnapshotRepository


def _attempt(
    snapshot_id: str = "snap-1",
    *,
    deployment_id: str = "smh_short_lane",
    symbol: str = "SMH",
    lane: str = "live",
    selector_empty: bool = False,
    selected_option_symbol: str | None = "SMH260708P00250000",
    rows: list[ContractSnapshotRow] | None = None,
) -> ChainSnapshotAttempt:
    if rows is None:
        rows = [
            ContractSnapshotRow(
                option_symbol="SMH260708P00250000",
                expiration_date="2026-07-08",
                dte=0,
                strike=250.0,
                contract_type="PUT",
                open_interest=500,
                delta=-0.25,
                bid=3.00,
                ask=3.10,
                spread_pct=0.0328,
                dte_in_window=True,
                verdict="accepted",
                fallback_verdict=None,
                is_selected=True,
            ),
            ContractSnapshotRow(
                option_symbol="SMH260708P00240000",
                expiration_date="2026-07-08",
                dte=0,
                strike=240.0,
                contract_type="PUT",
                open_interest=50,
                delta=-0.20,
                bid=2.00,
                ask=2.20,
                spread_pct=0.095,
                dte_in_window=True,
                verdict="open_interest_below_min",
                fallback_verdict=None,
                is_selected=False,
            ),
        ]
    return ChainSnapshotAttempt(
        snapshot_id=snapshot_id,
        deployment_id=deployment_id,
        symbol=symbol,
        lane=lane,
        direction="short",
        allowed_contract_type="PUT",
        dte_min=0,
        dte_max=1,
        min_open_interest=100,
        target_abs_delta_min=0.15,
        target_abs_delta_max=0.35,
        max_bid_ask_spread_pct=0.10,
        dte_fallback_policy="strict",
        nearest_after_dte=None,
        total_candidates=len(rows),
        captured_candidates=len(rows),
        selector_empty=selector_empty,
        selected_option_symbol=selected_option_symbol,
        option_candidate_set_sha256="a" * 64,
        actual_option_selection_sha256="b" * 64,
        rows=rows,
    )


def test_record_attempt_writes_summary_and_per_contract_rows(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    repo = SQLiteChainSnapshotRepository(str(db_path))

    asyncio.run(repo.record_attempt(_attempt()))

    with closing(sqlite3.connect(str(db_path))) as conn:
        attempt_row = conn.execute(
            "SELECT deployment_id, symbol, lane, selector_empty, selected_option_symbol, captured_candidates, "
            "option_candidate_set_sha256, actual_option_selection_sha256 "
            "FROM option_chain_snapshot_attempts WHERE snapshot_id = ?",
            ("snap-1",),
        ).fetchone()
        contract_rows = conn.execute(
            "SELECT option_symbol, verdict, is_selected, attempt_selected_option_symbol "
            "FROM option_chain_snapshots WHERE snapshot_id = ? ORDER BY option_symbol",
            ("snap-1",),
        ).fetchall()

    assert attempt_row == (
        "smh_short_lane",
        "SMH",
        "live",
        0,
        "SMH260708P00250000",
        2,
        "a" * 64,
        "b" * 64,
    )
    assert len(contract_rows) == 2
    accepted_row = next(row for row in contract_rows if row[0] == "SMH260708P00250000")
    assert accepted_row[1] == "accepted"
    assert accepted_row[2] == 1
    assert accepted_row[3] == "SMH260708P00250000"
    rejected_row = next(row for row in contract_rows if row[0] == "SMH260708P00240000")
    assert rejected_row[1] == "open_interest_below_min"
    assert rejected_row[2] == 0


def test_record_attempt_with_selector_empty_and_no_rows(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    repo = SQLiteChainSnapshotRepository(str(db_path))

    attempt = _attempt("snap-empty", selector_empty=True, selected_option_symbol=None, rows=[])

    asyncio.run(repo.record_attempt(attempt))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute(
            "SELECT selector_empty, selected_option_symbol, captured_candidates "
            "FROM option_chain_snapshot_attempts WHERE snapshot_id = ?",
            ("snap-empty",),
        ).fetchone()
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM option_chain_snapshots WHERE snapshot_id = ?",
            ("snap-empty",),
        ).fetchone()[0]

    assert row == (1, None, 0)
    assert contract_count == 0


def test_purge_older_than_deletes_only_stale_rows(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    repo = SQLiteChainSnapshotRepository(str(db_path), backend=backend)

    asyncio.run(repo.record_attempt(_attempt("snap-old")))
    asyncio.run(repo.record_attempt(_attempt("snap-new")))

    # Backdate the "old" attempt's rows past the retention cutoff directly --
    # record_attempt always stamps "now", so simulate age by rewriting
    # created_at the way a 40-day-old row would look.
    stale_created_at = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    with closing(backend.connect()) as conn:
        conn.execute(
            "UPDATE option_chain_snapshot_attempts SET created_at = ? WHERE snapshot_id = ?",
            (stale_created_at, "snap-old"),
        )
        conn.execute(
            "UPDATE option_chain_snapshots SET created_at = ? WHERE snapshot_id = ?",
            (stale_created_at, "snap-old"),
        )
        conn.commit()

    cutoff = datetime.now(UTC) - timedelta(days=30)
    deleted = asyncio.run(repo.purge_older_than(cutoff))

    with closing(sqlite3.connect(str(db_path))) as conn:
        remaining_attempts = {
            row[0] for row in conn.execute("SELECT snapshot_id FROM option_chain_snapshot_attempts").fetchall()
        }
        remaining_contracts = {
            row[0] for row in conn.execute("SELECT snapshot_id FROM option_chain_snapshots").fetchall()
        }

    assert remaining_attempts == {"snap-new"}
    assert remaining_contracts == {"snap-new"}
    # 1 stale attempt summary row + 2 stale per-contract rows.
    assert deleted == 3


def test_chain_snapshot_repository_shares_backend_write_lock(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    repo = SQLiteChainSnapshotRepository(str(db_path), backend=backend)

    async def run() -> None:
        await asyncio.gather(*(repo.record_attempt(_attempt(f"snap-{i}")) for i in range(10)))

    asyncio.run(run())

    with closing(sqlite3.connect(str(db_path))) as conn:
        count = conn.execute("SELECT COUNT(*) FROM option_chain_snapshot_attempts").fetchone()[0]
    assert count == 10
