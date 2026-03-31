from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from bhiksha.app.replay import ReplayTrade
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.tools.trade_replay import _trade_to_csv_row, _write_csv


def test_trade_replay_writes_entry_and_exit_csv(tmp_path: Path) -> None:
    trade = ReplayTrade(
        entry_index=0,
        entry_decision=SignalDecision(
            deployment_id="market_impulse_qqq_short_v1",
            symbol="QQQ",
            timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
            signal=True,
            direction=SignalDirection.SHORT,
            reason=["time_window_ok", "cross_and_reclaim_short"],
        ),
        exit_index=1,
        exit_decision=ExitDecision(
            deployment_id="market_impulse_qqq_short_v1",
            symbol="QQQ",
            timestamp=datetime(2026, 3, 30, 19, 55, tzinfo=UTC),
            exit=True,
            action="square_off",
            reason=["hard_flat_time_reached"],
        ),
        exit_category="time_exit",
    )
    enriched = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                datetime(2026, 3, 30, 19, 55, tzinfo=UTC),
            ],
            "open": [100.2, 99.8],
            "close": [99.9, 99.7],
        }
    )

    row = _trade_to_csv_row(trade, enriched)
    output_path = tmp_path / "trade_replay.csv"
    _write_csv(output_path, [row])

    content = output_path.read_text(encoding="utf-8")

    assert "deployment_id,symbol,entry_timestamp_et,entry_date_ct,entry_time_ct,entry_direction,entry_bar_open,entry_bar_close,entry_reason_json,exit_category,premium_exit_status,exit_timestamp_et,exit_date_ct,exit_time_ct,exit_bar_open,exit_bar_close,exit_action,exit_reason_json,underlying_move,underlying_move_pct,thesis_outcome,holding_bars" in content
    assert "market_impulse_qqq_short_v1" in content
    assert "09:30:00 AM CDT" in content
    assert "02:55:00 PM CDT" in content
    assert "not_available" in content
    assert "-0.2" in content
    assert "-0.002002" in content
    assert "favorable" in content
