import asyncio

from bhiksha.execution.order_manager import OrderManager, normalize_option_symbol, round_price, snap_price


def test_option_symbol_normalization_strips_suffix() -> None:
    assert normalize_option_symbol("qqq250330p00100000-option") == "QQQ250330P00100000"


def test_round_price_uses_two_decimals() -> None:
    assert round_price(1.234) == 1.23


def test_snap_price_uses_buy_ceiling() -> None:
    assert snap_price(3.21, 0.10, side="BUY") == 3.30


def test_snap_price_uses_sell_floor() -> None:
    assert snap_price(3.21, 0.10, side="SELL") == 3.20


def test_order_manager_snaps_target_and_stop_prices_using_preflight_increment() -> None:
    class StubBroker:
        def __init__(self) -> None:
            self.placed_orders: list[dict] = []

        async def preflight_single_leg(self, payload: dict) -> dict:
            return {"priceIncrement": {"currentIncrement": "0.10"}}

        async def place_order(self, payload: dict) -> dict:
            self.placed_orders.append(payload)
            return {"orderId": "OID123"}

        async def close(self) -> None:
            return None

    broker = StubBroker()
    manager = OrderManager(broker=broker)

    asyncio.run(manager.place_target_order("QQQ260401P00556000", 3.29, 1))
    asyncio.run(manager.place_stop_loss_order("QQQ260401P00556000", 1.17, 1))

    assert broker.placed_orders[0]["limitPrice"] == "3.20"
    assert broker.placed_orders[1]["stopPrice"] == "1.10"
