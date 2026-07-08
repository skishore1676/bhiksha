"""Analytics reader for option_chain_snapshot* tables.

Turns the per-contract chain captures written by
``bhiksha.persistence.sqlite.SQLiteChainSnapshotRepository`` (see
``bhiksha.options.chain_snapshot`` for what gets captured) into the two
questions the capture exists to answer:

  * ``rejection_waterfall`` -- reproduce the per-filter elimination counts
    (accepted / dte_out_of_range / open_interest_below_min / ...) across a
    date range, the same buckets ``SelectorEmptyError.breakdown`` reports for
    a single attempt, but aggregated.
  * ``what_if_oi_floor`` -- pure arithmetic replay of "would a different
    open-interest floor have changed the outcome?" using only the raw
    per-contract attributes already captured (open_interest/delta/spread) and
    the attempt's own delta/spread thresholds. No new chain fetch.

Read-only: this module never writes to the database.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
import sqlite3
from pathlib import Path
from typing import Any

from bhiksha.options.chain_snapshot import VERDICT_ACCEPTED, VERDICT_OI_BELOW_MIN


@dataclass(slots=True, frozen=True)
class WaterfallResult:
    start_date: str
    end_date: str
    deployment_id: str | None
    symbol: str | None
    attempt_count: int
    selector_empty_count: int
    selected_count: int
    total_candidates_sum: int
    captured_candidates_sum: int
    verdict_counts: dict[str, int] = field(default_factory=dict)
    fallback_verdict_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "deployment_id": self.deployment_id,
            "symbol": self.symbol,
            "attempt_count": self.attempt_count,
            "selector_empty_count": self.selector_empty_count,
            "selected_count": self.selected_count,
            "total_candidates_sum": self.total_candidates_sum,
            "captured_candidates_sum": self.captured_candidates_sum,
            "verdict_counts": dict(self.verdict_counts),
            "fallback_verdict_counts": dict(self.fallback_verdict_counts),
        }


def rejection_waterfall(
    db_path: str | Path,
    *,
    start_date: date | str,
    end_date: date | str,
    deployment_id: str | None = None,
    symbol: str | None = None,
) -> WaterfallResult:
    """Reproduce the per-contract rejection waterfall for a date range.

    ``verdict_counts`` is keyed the same way as ``SelectorEmptyError``'s
    ``breakdown`` (``accepted``, ``dte_out_of_range``,
    ``open_interest_below_min``, ``delta_below_min``, ``delta_above_max``,
    ``spread_above_max``) but summed across every captured contract row in
    range -- window rows only. ``fallback_verdict_counts`` covers the
    nearest-after-DTE rows captured alongside the window (see
    options/chain_snapshot.py), answering "would the fallback expiry have
    passed" independent of whether the fallback policy was actually active.
    """
    start_iso, end_iso = _day_bounds(start_date, end_date)
    filters, filter_params = _optional_filters(deployment_id=deployment_id, symbol=symbol)

    with closing(sqlite3.connect(str(db_path))) as conn:
        attempt_rows = conn.execute(
            f"""
            SELECT selector_empty, selected_option_symbol, total_candidates, captured_candidates
            FROM option_chain_snapshot_attempts
            WHERE created_at BETWEEN ? AND ? {filters}
            """,
            (start_iso, end_iso, *filter_params),
        ).fetchall()

        contract_rows = conn.execute(
            f"""
            SELECT verdict, fallback_verdict
            FROM option_chain_snapshots
            WHERE created_at BETWEEN ? AND ? {filters}
            """,
            (start_iso, end_iso, *filter_params),
        ).fetchall()

    verdict_counts: Counter[str] = Counter()
    fallback_verdict_counts: Counter[str] = Counter()
    for verdict, fallback_verdict in contract_rows:
        verdict_counts[verdict] += 1
        if fallback_verdict is not None:
            fallback_verdict_counts[fallback_verdict] += 1

    attempt_count = len(attempt_rows)
    selector_empty_count = sum(1 for row in attempt_rows if row[0])
    selected_count = sum(1 for row in attempt_rows if row[1])
    total_candidates_sum = sum(row[2] or 0 for row in attempt_rows)
    captured_candidates_sum = sum(row[3] or 0 for row in attempt_rows)

    return WaterfallResult(
        start_date=str(start_date),
        end_date=str(end_date),
        deployment_id=deployment_id,
        symbol=symbol,
        attempt_count=attempt_count,
        selector_empty_count=selector_empty_count,
        selected_count=selected_count,
        total_candidates_sum=total_candidates_sum,
        captured_candidates_sum=captured_candidates_sum,
        verdict_counts=dict(verdict_counts),
        fallback_verdict_counts=dict(fallback_verdict_counts),
    )


@dataclass(slots=True, frozen=True)
class OiFloorWhatIf:
    symbol: str
    oi_floor: int
    window_rows_considered: int
    window_rows_would_accept: int
    fallback_rows_considered: int
    fallback_rows_would_accept: int


def what_if_oi_floor(
    db_path: str | Path,
    *,
    start_date: date | str,
    end_date: date | str,
    symbol: str,
    oi_floor: int,
    deployment_id: str | None = None,
) -> OiFloorWhatIf:
    """Recompute "would this OI floor have passed" per captured contract row.

    Only rows whose recorded verdict/fallback_verdict is already
    ``accepted`` or ``open_interest_below_min`` are considered -- those are
    exactly the rows where OI was the only thing the real cascade checked
    before/at the point of failure (delta and spread already passed, or OI
    failed first). Rows that failed on delta or spread cannot be rescued by
    a different OI floor, so they are excluded either way. No new chain
    fetch and no re-running the selector -- just the stored ``open_interest``
    compared against ``oi_floor``.
    """
    start_iso, end_iso = _day_bounds(start_date, end_date)
    filters, filter_params = _optional_filters(deployment_id=deployment_id, alias="s")

    window_sql = f"""
        SELECT s.open_interest
        FROM option_chain_snapshots s
        WHERE s.created_at BETWEEN ? AND ?
          AND s.symbol = ?
          AND s.dte_in_window = 1
          AND s.verdict IN (?, ?)
          {filters}
    """
    fallback_sql = f"""
        SELECT s.open_interest
        FROM option_chain_snapshots s
        WHERE s.created_at BETWEEN ? AND ?
          AND s.symbol = ?
          AND s.dte_in_window = 0
          AND s.fallback_verdict IN (?, ?)
          {filters}
    """
    base_params = (start_iso, end_iso, symbol, VERDICT_ACCEPTED, VERDICT_OI_BELOW_MIN, *filter_params)

    with closing(sqlite3.connect(str(db_path))) as conn:
        window_rows = conn.execute(window_sql, base_params).fetchall()
        fallback_rows = conn.execute(fallback_sql, base_params).fetchall()

    window_accept = sum(1 for (open_interest,) in window_rows if (open_interest or 0) >= oi_floor)
    fallback_accept = sum(1 for (open_interest,) in fallback_rows if (open_interest or 0) >= oi_floor)

    return OiFloorWhatIf(
        symbol=symbol,
        oi_floor=oi_floor,
        window_rows_considered=len(window_rows),
        window_rows_would_accept=window_accept,
        fallback_rows_considered=len(fallback_rows),
        fallback_rows_would_accept=fallback_accept,
    )


def _day_bounds(start_date: date | str, end_date: date | str) -> tuple[str, str]:
    start = start_date if isinstance(start_date, date) else date.fromisoformat(str(start_date))
    end = end_date if isinstance(end_date, date) else date.fromisoformat(str(end_date))
    return f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59.999999"


def _optional_filters(*, deployment_id: str | None = None, symbol: str | None = None, alias: str | None = None) -> tuple[str, tuple]:
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    if deployment_id is not None:
        clauses.append(f"AND {prefix}deployment_id = ?")
        params.append(deployment_id)
    if symbol is not None:
        clauses.append(f"AND {prefix}symbol = ?")
        params.append(symbol)
    return (" ".join(clauses), tuple(params))
