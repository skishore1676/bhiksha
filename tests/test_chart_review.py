from datetime import UTC, datetime
from pathlib import Path

from bhiksha.domain.models import Bar
from bhiksha.tools.chart_review import (
    _contract_type,
    _copy_static_assets,
    _is_regular_session_bar,
    _symbol_payload,
)
from bhiksha.tools.export_thinkorswim_study import ThinkorswimTrade


def test_symbol_payload_pairs_trade_markers_with_underlying_bars() -> None:
    trade = ThinkorswimTrade(
        trade_id="trade-123456",
        deployment_id="strategy_market_impulse_tsla_short_live_row_5",
        symbol="TSLA",
        option_symbol="TSLA260501P00365000",
        quantity=4,
        entry_price=2.16,
        underlying_entry_price=371.45,
        entry_timestamp=datetime(2026, 4, 30, 13, 45, 8, tzinfo=UTC),
        status="closed",
        stop_price=1.2,
        target_price=None,
        exit_mode="strategy",
        exit_price=1.2,
        exit_filled_at=datetime(2026, 4, 30, 14, 46, 22, tzinfo=UTC),
    )
    bars = [
        _bar("TSLA", datetime(2026, 4, 30, 13, 45, tzinfo=UTC), 371.0, 372.0, 370.5, 371.5),
        _bar("TSLA", datetime(2026, 4, 30, 14, 46, tzinfo=UTC), 368.0, 368.4, 367.5, 368.2),
    ]

    payload = _symbol_payload([trade], bars)

    serialized = payload["trades"][0]
    assert serialized["direction"] == "short"
    assert serialized["contractType"] == "PUT"
    assert serialized["entryAction"] == "BUY"
    assert serialized["exitAction"] == "SELL"
    assert serialized["entryMarkerTime"] == int(bars[0].timestamp.timestamp())
    assert serialized["exitMarkerTime"] == int(bars[1].timestamp.timestamp())
    assert serialized["underlyingExitApprox"] == 368.2
    assert serialized["optionPnl"] == -384.0
    assert payload["warnings"] == []


def test_symbol_payload_warns_when_marker_bar_is_missing() -> None:
    trade = ThinkorswimTrade(
        trade_id="trade-abcdef",
        deployment_id="strategy_elastic_band_reversion_nvda_long_live_row_2",
        symbol="NVDA",
        option_symbol="NVDA260515C00217500",
        quantity=1,
        entry_price=3.0,
        underlying_entry_price=208.0,
        entry_timestamp=datetime(2026, 4, 30, 13, 35, tzinfo=UTC),
        status="open",
        stop_price=None,
        target_price=None,
        exit_mode=None,
        exit_price=None,
        exit_filled_at=None,
    )

    payload = _symbol_payload([trade], [])

    assert payload["trades"][0]["entryMarkerTime"] is None
    assert payload["warnings"] == ["Missing entry candle for NVDA trade trade-ab."]


def test_copy_static_assets(tmp_path: Path) -> None:
    _copy_static_assets(tmp_path)

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "app.js").exists()
    assert (tmp_path / "styles.css").exists()


def test_contract_type_parses_standard_option_symbols() -> None:
    assert _contract_type("TSLA260501P00365000") == "PUT"
    assert _contract_type("NVDA260515C00217500") == "CALL"
    assert _contract_type("TSLA") is None


def test_regular_session_filter_uses_eastern_market_hours() -> None:
    assert not _is_regular_session_bar(
        _bar("TSLA", datetime(2026, 4, 30, 13, 29, tzinfo=UTC), 1, 1, 1, 1)
    )
    assert _is_regular_session_bar(
        _bar("TSLA", datetime(2026, 4, 30, 13, 30, tzinfo=UTC), 1, 1, 1, 1)
    )


def _bar(symbol: str, timestamp: datetime, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )
