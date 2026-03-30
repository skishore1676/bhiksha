from bhiksha.execution.order_manager import normalize_option_symbol, round_price, snap_price


def test_option_symbol_normalization_strips_suffix() -> None:
    assert normalize_option_symbol("qqq250330p00100000-option") == "QQQ250330P00100000"


def test_round_price_uses_two_decimals() -> None:
    assert round_price(1.234) == 1.23


def test_snap_price_uses_buy_ceiling() -> None:
    assert snap_price(3.21, 0.10, side="BUY") == 3.30


def test_snap_price_uses_sell_floor() -> None:
    assert snap_price(3.21, 0.10, side="SELL") == 3.20
