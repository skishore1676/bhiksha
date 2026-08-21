from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bhiksha.execution.cartographer_excursions import option_mfe_mae, underlying_mfe_mae


def test_option_excursions_are_trade_keyed_and_entry_to_exit_complete() -> None:
    entry = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    exit_at = entry + timedelta(minutes=2)
    result = option_mfe_mae(
        trade_id="t1",
        entry_at=entry,
        exit_at=exit_at,
        entry_price=10.0,
        marks=[
            {"trade_id": "other", "timestamp": entry, "price": 99, "coverage": "complete"},
            {"trade_id": "t1", "timestamp": entry, "price": 10, "coverage": "complete"},
            {"trade_id": "t1", "timestamp": entry + timedelta(minutes=1), "price": 12, "coverage": "complete"},
            {"trade_id": "t1", "timestamp": exit_at, "price": 9, "coverage": "complete"},
            {"trade_id": "t1", "timestamp": exit_at + timedelta(minutes=1), "price": 50, "coverage": "complete"},
        ],
    )
    assert result == {
        "coverage": "complete", "coverage_reasons": [], "mfe_pct": 0.2, "mae_pct": -0.1
    }


def test_partial_or_missing_marks_never_become_zero_excursions() -> None:
    entry = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    result = option_mfe_mae(
        trade_id="t1", entry_at=entry, exit_at=entry + timedelta(minutes=1), entry_price=10, marks=[]
    )
    assert result["coverage"] == "missing"
    assert result["mfe_pct"] is None and result["mae_pct"] is None


def test_underlying_entry_bar_is_partial_and_short_uses_opposite_geometry() -> None:
    entry = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    partial = underlying_mfe_mae(
        direction="long", entry_at=entry, exit_at=entry + timedelta(minutes=2), entry_price=100,
        bars=[{"start": entry - timedelta(minutes=1), "end": entry + timedelta(minutes=1), "high": 110, "low": 90, "coverage": "complete"}],
    )
    assert partial["coverage"] == "partial"
    complete = underlying_mfe_mae(
        direction="short", entry_at=entry, exit_at=entry + timedelta(minutes=2), entry_price=100,
        bars=[
            {"start": entry, "end": entry + timedelta(minutes=1), "high": 103, "low": 95, "coverage": "complete"},
            {"start": entry + timedelta(minutes=1), "end": entry + timedelta(minutes=2), "high": 101, "low": 90, "coverage": "complete"},
        ],
    )
    assert complete["mfe_pct"] == round(100 / 90 - 1, 8)
    assert complete["mae_pct"] == round(100 / 103 - 1, 8)


def test_gaps_make_excursion_coverage_partial() -> None:
    entry = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    option = option_mfe_mae(
        trade_id="t1",
        entry_at=entry,
        exit_at=entry + timedelta(minutes=3),
        entry_price=10,
        marks=[
            {"trade_id": "t1", "timestamp": entry, "price": 10, "coverage": "complete"},
            {"trade_id": "t1", "timestamp": entry + timedelta(minutes=3), "price": 11, "coverage": "complete"},
        ],
    )
    assert option["coverage"] == "partial"
    assert option["coverage_reasons"] == ["option_mark_gap"]

    underlying = underlying_mfe_mae(
        direction="long",
        entry_at=entry,
        exit_at=entry + timedelta(minutes=3),
        entry_price=100,
        bars=[
            {"start": entry, "end": entry + timedelta(minutes=1), "high": 101, "low": 99, "coverage": "complete"},
            {"start": entry + timedelta(minutes=2), "end": entry + timedelta(minutes=3), "high": 102, "low": 98, "coverage": "complete"},
        ],
    )
    assert underlying["coverage"] == "partial"
    assert underlying["coverage_reasons"] == ["underlying_bar_gap"]
