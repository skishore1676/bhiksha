import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta

from bhiksha.options.chain_snapshot import ChainSnapshotAttempt, ContractSnapshotRow
from bhiksha.ops.chain_snapshot_report import rejection_waterfall, what_if_oi_floor
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteChainSnapshotRepository


def _row(
    option_symbol: str,
    *,
    open_interest: int,
    dte_in_window: bool,
    verdict: str,
    fallback_verdict: str | None = None,
    is_selected: bool = False,
) -> ContractSnapshotRow:
    return ContractSnapshotRow(
        option_symbol=option_symbol,
        expiration_date="2026-07-07",
        dte=0 if dte_in_window else 9,
        strike=250.0,
        contract_type="PUT",
        open_interest=open_interest,
        delta=-0.25,
        bid=3.00,
        ask=3.10,
        spread_pct=0.033,
        dte_in_window=dte_in_window,
        verdict=verdict,
        fallback_verdict=fallback_verdict,
        is_selected=is_selected,
    )


def _attempt(
    snapshot_id: str,
    *,
    deployment_id: str = "smh_short_lane",
    symbol: str = "SMH",
    selector_empty: bool,
    selected_option_symbol: str | None,
    rows: list[ContractSnapshotRow],
) -> ChainSnapshotAttempt:
    return ChainSnapshotAttempt(
        snapshot_id=snapshot_id,
        deployment_id=deployment_id,
        symbol=symbol,
        lane="live",
        direction="short",
        allowed_contract_type="PUT",
        dte_min=0,
        dte_max=1,
        min_open_interest=100,
        target_abs_delta_min=0.15,
        target_abs_delta_max=0.35,
        max_bid_ask_spread_pct=0.10,
        dte_fallback_policy="strict",
        nearest_after_dte=9 if any(not row.dte_in_window for row in rows) else None,
        total_candidates=len(rows),
        captured_candidates=len(rows),
        selector_empty=selector_empty,
        selected_option_symbol=selected_option_symbol,
        rows=rows,
    )


def _seed(db_path, backend: SQLiteBackend) -> None:
    repo = SQLiteChainSnapshotRepository(str(db_path), backend=backend)

    # Attempt A (SMH, in range): one accepted (selected) + one OI-rejected.
    attempt_a = _attempt(
        "snap-a",
        selector_empty=False,
        selected_option_symbol="SMH_ACCEPTED",
        rows=[
            _row("SMH_ACCEPTED", open_interest=500, dte_in_window=True, verdict="accepted", is_selected=True),
            _row("SMH_LOW_OI", open_interest=50, dte_in_window=True, verdict="open_interest_below_min"),
        ],
    )
    # Attempt B (SMH, in range): selector-empty, one window OI-rejected row
    # plus one nearest-after fallback row.
    attempt_b = _attempt(
        "snap-b",
        selector_empty=True,
        selected_option_symbol=None,
        rows=[
            _row("SMH_WINDOW_LOW_OI", open_interest=80, dte_in_window=True, verdict="open_interest_below_min"),
            _row(
                "SMH_FALLBACK",
                open_interest=60,
                dte_in_window=False,
                verdict="dte_out_of_range",
                fallback_verdict="open_interest_below_min",
            ),
        ],
    )
    # Attempt C: different symbol/deployment -- must not leak into SMH-scoped queries.
    attempt_c = _attempt(
        "snap-c",
        deployment_id="qqq_short_lane",
        symbol="QQQ",
        selector_empty=False,
        selected_option_symbol="QQQ_ACCEPTED",
        rows=[_row("QQQ_ACCEPTED", open_interest=300, dte_in_window=True, verdict="accepted", is_selected=True)],
    )
    # Attempt D: SMH but will be backdated outside the query range.
    attempt_d = _attempt(
        "snap-d-old",
        selector_empty=False,
        selected_option_symbol="SMH_OLD",
        rows=[_row("SMH_OLD", open_interest=500, dte_in_window=True, verdict="accepted", is_selected=True)],
    )

    for attempt in (attempt_a, attempt_b, attempt_c, attempt_d):
        asyncio.run(repo.record_attempt(attempt))

    old_created_at = (datetime(2026, 7, 1, tzinfo=UTC)).isoformat()
    with closing(backend.connect()) as conn:
        conn.execute(
            "UPDATE option_chain_snapshot_attempts SET created_at = ? WHERE snapshot_id = ?",
            (old_created_at, "snap-d-old"),
        )
        conn.execute(
            "UPDATE option_chain_snapshots SET created_at = ? WHERE snapshot_id = ?",
            (old_created_at, "snap-d-old"),
        )
        conn.commit()


def test_rejection_waterfall_reproduces_verdict_counts_in_range(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    _seed(db_path, backend)

    today = datetime.now(UTC).date()
    result = rejection_waterfall(db_path, start_date=today, end_date=today, symbol="SMH")

    assert result.attempt_count == 2  # snap-a, snap-b (snap-d-old is out of range)
    assert result.selector_empty_count == 1
    assert result.selected_count == 1
    assert result.verdict_counts == {"accepted": 1, "open_interest_below_min": 2, "dte_out_of_range": 1}
    assert result.fallback_verdict_counts == {"open_interest_below_min": 1}


def test_rejection_waterfall_date_range_excludes_old_attempt(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    _seed(db_path, backend)

    result = rejection_waterfall(db_path, start_date="2026-06-01", end_date="2026-07-02", symbol="SMH")

    assert result.attempt_count == 1
    assert result.selected_count == 1
    assert result.verdict_counts == {"accepted": 1}


def test_rejection_waterfall_deployment_filter_isolates_symbol(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    _seed(db_path, backend)

    today = datetime.now(UTC).date()
    result = rejection_waterfall(db_path, start_date=today, end_date=today, deployment_id="qqq_short_lane")

    assert result.attempt_count == 1
    assert result.symbol is None  # filter is by deployment_id, not symbol -- confirms no SMH leakage
    assert result.verdict_counts == {"accepted": 1}


def test_what_if_oi_floor_recomputes_from_raw_open_interest(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    _seed(db_path, backend)

    today = datetime.now(UTC).date()

    lower_floor = what_if_oi_floor(db_path, start_date=today, end_date=today, symbol="SMH", oi_floor=60)
    # Window candidates considered: SMH_ACCEPTED(500), SMH_LOW_OI(50), SMH_WINDOW_LOW_OI(80).
    assert lower_floor.window_rows_considered == 3
    assert lower_floor.window_rows_would_accept == 2  # 500 and 80 clear a floor of 60; 50 does not
    assert lower_floor.fallback_rows_considered == 1  # SMH_FALLBACK(60)
    assert lower_floor.fallback_rows_would_accept == 1  # 60 >= 60

    higher_floor = what_if_oi_floor(db_path, start_date=today, end_date=today, symbol="SMH", oi_floor=100)
    assert higher_floor.window_rows_would_accept == 1  # only 500 clears 100
    assert higher_floor.fallback_rows_would_accept == 0  # 60 < 100


def test_what_if_oi_floor_deployment_filter_excludes_other_deployments(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    _seed(db_path, backend)

    today = datetime.now(UTC).date()

    # qqq_short_lane never touched SMH -- filtering by a deployment_id that
    # has no SMH rows must zero out the SMH-scoped what-if, not silently
    # ignore the filter and fall back to every deployment's SMH rows.
    result = what_if_oi_floor(
        db_path,
        start_date=today,
        end_date=today,
        symbol="SMH",
        oi_floor=50,
        deployment_id="qqq_short_lane",
    )
    assert result.window_rows_considered == 0
    assert result.fallback_rows_considered == 0

    scoped = what_if_oi_floor(
        db_path,
        start_date=today,
        end_date=today,
        symbol="SMH",
        oi_floor=50,
        deployment_id="smh_short_lane",
    )
    assert scoped.window_rows_considered == 3
    assert scoped.fallback_rows_considered == 1
