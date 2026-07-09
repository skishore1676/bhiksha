"""Tests for the daily shadow-EV report (ops/shadow_ev_report.py).

These exercise the report against a fixture SQLite DB seeded through the REAL
persistence repositories, so the EV math (including banked partial-scale legs),
lane grouping, live-trade exclusion, and rendering are checked against the same
schema the runtime writes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import PartialFillRecord, TradeRecord
from bhiksha.ops.shadow_ev_report import (
    build_shadow_ev_report,
    render_shadow_ev_report_markdown,
    render_shadow_ev_report_telegram,
    write_shadow_ev_report,
)
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteTradeStateRepository

SINCE = "2026-07-02"


def _ts(day: int, hour: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=UTC)


def _shadow_trade(
    trade_id: str,
    deployment_id: str,
    *,
    entry: float,
    exit_price: float,
    quantity: int,
    exit_qty: int | None = None,
    close_day: int = 8,
    exit_rule: str | None = None,
    exit_mode: ExitMode | None = None,
    entry_order_id: str = "SHADOW_ENTRY",
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        deployment_id=deployment_id,
        symbol=deployment_id.split("_")[0].upper(),
        option_symbol="ACME260717C00100000",
        quantity=quantity,
        entry_price=entry,
        entry_timestamp=_ts(close_day, hour=9),
        status="closed",
        entry_order_id=entry_order_id,
        exit_price=exit_price,
        exit_filled_quantity=exit_qty if exit_qty is not None else quantity,
        exit_filled_at=_ts(close_day),
        exit_mode=exit_mode,
        exit_rule=exit_rule,
    )


def _seed(db_path, trades: list[TradeRecord], partials: list[PartialFillRecord] | None = None) -> None:
    backend = SQLiteBackend(str(db_path))
    repo = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def run() -> None:
        for trade in trades:
            await repo.upsert_trade(trade)
        for partial in partials or []:
            await repo.record_partial_fill(partial)

    asyncio.run(run())


def test_ev_math_includes_partial_legs(tmp_path) -> None:
    """Full realized P&L = residual runner leg + every banked partial leg."""
    db_path = tmp_path / "bhiksha.db"
    # Runner residual: entry 1.00 -> exit 1.50, 1 contract => (0.50)*1*100 = $50.
    trade = _shadow_trade(
        "t-partial", "spy_long_shadow_row_1", entry=1.00, exit_price=1.50, quantity=1
    )
    # Banked partial: entry 1.00 -> fill 2.00, 1 contract => (1.00)*1*100 = $100.
    partial = PartialFillRecord(
        id=None,
        trade_id="t-partial",
        deployment_id="spy_long_shadow_row_1",
        symbol="SPY",
        option_symbol="ACME260717C00100000",
        closed_quantity=1,
        order_id="paper-partial-1",
        exit_rule="target_1_partial",
        submitted_at=_ts(8),
        fill_price=2.00,
        fill_quantity=1,
        filled_at=_ts(8),
    )
    _seed(db_path, [trade], [partial])

    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    lane = report["lanes"][0]
    # $50 runner + $100 partial = $150 for the single round trip.
    assert lane["since"]["total_pnl_usd"] == 150.0
    assert lane["since"]["ev_per_trade_usd"] == 150.0
    assert lane["since"]["trades"] == 1
    assert report["book"]["since"]["total_pnl_usd"] == 150.0


def test_partial_legs_ignored_when_fill_price_missing(tmp_path) -> None:
    """An unconfirmed partial leg (no fill_price) must not inflate paper P&L."""
    db_path = tmp_path / "bhiksha.db"
    trade = _shadow_trade("t-x", "spy_long_shadow_row_1", entry=1.00, exit_price=1.50, quantity=1)
    unconfirmed = PartialFillRecord(
        id=None,
        trade_id="t-x",
        deployment_id="spy_long_shadow_row_1",
        symbol="SPY",
        option_symbol="ACME260717C00100000",
        closed_quantity=1,
        order_id="paper-partial-x",
        exit_rule="target_1_partial",
        submitted_at=_ts(8),
        fill_price=None,
    )
    _seed(db_path, [trade], [unconfirmed])
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    assert report["book"]["since"]["total_pnl_usd"] == 50.0


def test_groups_by_lane_and_excludes_live(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = [
        _shadow_trade("s1", "meta_short_shadow_row_1", entry=2.0, exit_price=1.0, quantity=1),  # -100
        _shadow_trade("s2", "meta_short_shadow_row_1", entry=2.0, exit_price=3.0, quantity=1),  # +100
        _shadow_trade("s3", "aapl_long_shadow_row_2", entry=1.0, exit_price=2.0, quantity=1),   # +100
        # A LIVE trade (real order id) must be excluded entirely.
        _shadow_trade(
            "live1", "aapl_long_live", entry=1.0, exit_price=0.5, quantity=1, entry_order_id="ORD-123"
        ),
    ]
    _seed(db_path, trades)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))

    assert report["lane_count_traded"] == 2  # only the two shadow lanes
    assert report["book"]["since"]["trades"] == 3  # live trade excluded
    lanes = {lane["deployment_id"]: lane for lane in report["lanes"]}
    assert set(lanes) == {"meta_short_shadow_row_1", "aapl_long_shadow_row_2"}
    assert lanes["meta_short_shadow_row_1"]["since"]["total_pnl_usd"] == 0.0
    assert lanes["meta_short_shadow_row_1"]["since"]["wins"] == 1
    assert lanes["meta_short_shadow_row_1"]["since"]["losses"] == 1
    assert lanes["aapl_long_shadow_row_2"]["since"]["total_pnl_usd"] == 100.0


def test_win_rate_avg_win_avg_loss_and_ev(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    # Three trades on one lane: +100, +50, -30 => total 120, ev 40,
    # win_rate 2/3, avg_win 75, avg_loss -30.
    trades = [
        _shadow_trade("w1", "spy_long_shadow_row_1", entry=1.0, exit_price=2.0, quantity=1),   # +100
        _shadow_trade("w2", "spy_long_shadow_row_1", entry=1.0, exit_price=1.5, quantity=1),   # +50
        _shadow_trade("l1", "spy_long_shadow_row_1", entry=1.0, exit_price=0.7, quantity=1),   # -30
    ]
    _seed(db_path, trades)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    m = report["book"]["since"]
    assert m["trades"] == 3
    assert m["total_pnl_usd"] == 120.0
    assert m["ev_per_trade_usd"] == 40.0
    assert round(m["win_rate"], 4) == round(2 / 3, 4)
    assert m["avg_win_usd"] == 75.0
    assert m["avg_loss_usd"] == -30.0


def test_since_window_excludes_older_trades_but_all_time_keeps_them(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = [
        # Closed 2026-06-30 (before the since anchor): all-time only.
        TradeRecord(
            trade_id="old",
            deployment_id="spy_long_shadow_row_1",
            symbol="SPY",
            option_symbol="ACME260717C00100000",
            quantity=1,
            entry_price=1.0,
            entry_timestamp=datetime(2026, 6, 30, 9, tzinfo=UTC),
            status="closed",
            entry_order_id="SHADOW_ENTRY",
            exit_price=5.0,  # +400
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 30, 15, tzinfo=UTC),
        ),
        _shadow_trade("new", "spy_long_shadow_row_1", entry=1.0, exit_price=0.5, quantity=1),  # -50, 07-08
    ]
    _seed(db_path, trades)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    assert report["book"]["since"]["trades"] == 1
    assert report["book"]["since"]["total_pnl_usd"] == -50.0
    assert report["book"]["all_time"]["trades"] == 2
    assert report["book"]["all_time"]["total_pnl_usd"] == 350.0


def test_exit_rule_mix_labels(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = [
        _shadow_trade("a", "spy_long_shadow_row_1", entry=1.0, exit_price=1.1, quantity=1, exit_rule="no_progress"),
        _shadow_trade("b", "spy_long_shadow_row_1", entry=1.0, exit_price=1.1, quantity=1, exit_mode=ExitMode.HARD_FLAT),
        _shadow_trade("c", "spy_long_shadow_row_1", entry=1.0, exit_price=1.1, quantity=1),  # strategy_paper
    ]
    _seed(db_path, trades)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    mix = report["book"]["exit_mix"]
    assert mix.get("profile:no_progress") == 1
    assert mix.get("hard_flat") == 1
    assert mix.get("strategy_paper") == 1


def test_trend_flag_improving_and_degrading(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    # Older half loses, recent half wins => improving.
    improving = [
        _shadow_trade(f"i{i}", "up_shadow_row_1", entry=1.0, exit_price=(0.5 if i < 3 else 2.0), quantity=1, close_day=3 + i)
        for i in range(6)
    ]
    # Older half wins, recent half loses => degrading.
    degrading = [
        _shadow_trade(f"d{i}", "down_shadow_row_2", entry=1.0, exit_price=(2.0 if i < 3 else 0.5), quantity=1, close_day=3 + i)
        for i in range(6)
    ]
    _seed(db_path, improving + degrading)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(20, 20))
    lanes = {lane["deployment_id"]: lane for lane in report["lanes"]}
    assert lanes["up_shadow_row_1"]["trend"] == "improving"
    assert lanes["down_shadow_row_2"]["trend"] == "degrading"


def test_trend_flag_na_for_small_sample(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _seed(db_path, [_shadow_trade("only", "spy_long_shadow_row_1", entry=1.0, exit_price=1.5, quantity=1)])
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))
    assert report["lanes"][0]["trend"] == "n/a"


def test_rendering_is_short_honest_and_labeled(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = [
        _shadow_trade("s1", "meta_short_shadow_row_1", entry=2.0, exit_price=1.0, quantity=1),
        _shadow_trade("s2", "aapl_long_shadow_row_2", entry=1.0, exit_price=2.0, quantity=1),
    ]
    _seed(db_path, trades)
    report = build_shadow_ev_report(db_path, since=SINCE, now=_ts(9, 20))

    telegram = render_shadow_ev_report_telegram(report)
    assert "paper marks, not fills" in telegram
    assert "Shadow-EV" in telegram
    # Phone-friendly: the summary must stay short.
    assert len(telegram.splitlines()) <= 16

    markdown = render_shadow_ev_report_markdown(report)
    assert "Paper marks, not broker fills" in markdown
    assert "| Lane |" in markdown
    assert "meta_short" in markdown


def test_empty_db_returns_zeroed_report(tmp_path) -> None:
    report = build_shadow_ev_report(tmp_path / "does_not_exist.db", since=SINCE, now=_ts(9, 20))
    assert report["lane_count_traded"] == 0
    assert report["book"]["since"]["trades"] == 0
    assert report["book"]["since"]["total_pnl_usd"] == 0.0
    assert report["lanes"] == []
    # Rendering an empty report must not raise and must stay honest.
    assert "paper marks, not fills" in render_shadow_ev_report_telegram(report)


def test_write_report_emits_json_and_markdown(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _seed(db_path, [_shadow_trade("s1", "spy_long_shadow_row_1", entry=1.0, exit_price=2.0, quantity=1)])
    result = write_shadow_ev_report(db_path, output_dir=tmp_path / "reports", since=SINCE, now=_ts(9, 20))
    assert result.json_path.exists()
    assert result.markdown_path.exists()
    assert result.json_path.name == "shadow_ev_report_2026-07-09.json"
    assert result.report["book"]["since"]["total_pnl_usd"] == 100.0
