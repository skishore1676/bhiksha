import asyncio

import httpx

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


def test_order_manager_retries_entry_after_increment_rejection() -> None:
    class StubBroker:
        def __init__(self) -> None:
            self.placed_orders: list[dict] = []

        async def preflight_single_leg(self, payload: dict) -> dict:
            return {}

        async def place_order(self, payload: dict) -> dict:
            self.placed_orders.append(dict(payload))
            if len(self.placed_orders) == 1:
                raise ValueError("limitPrice must be in increments of $0.05")
            return {"orderId": "OID123"}

        async def close(self) -> None:
            return None

    broker = StubBroker()
    manager = OrderManager(broker=broker)

    result = asyncio.run(manager.place_entry_order("TSLA260410C00260000", 3.21, 1))

    assert result.order_id == "OID123"
    assert broker.placed_orders[0]["limitPrice"] == "3.21"
    assert broker.placed_orders[1]["limitPrice"] == "3.25"


def test_order_manager_retries_stop_after_increment_rejection() -> None:
    class StubBroker:
        def __init__(self) -> None:
            self.placed_orders: list[dict] = []

        async def preflight_single_leg(self, payload: dict) -> dict:
            return {}

        async def place_order(self, payload: dict) -> dict:
            self.placed_orders.append(dict(payload))
            if len(self.placed_orders) == 1:
                raise ValueError("stopPrice must be in increments of $0.05")
            return {"orderId": "STOP123"}

        async def close(self) -> None:
            return None

    broker = StubBroker()
    manager = OrderManager(broker=broker)

    result = asyncio.run(manager.place_stop_loss_order("TSLA260410C00260000", 1.18, 1))

    assert result.order_id == "STOP123"
    assert broker.placed_orders[0]["stopPrice"] == "1.18"
    assert broker.placed_orders[1]["stopPrice"] == "1.15"


def test_order_manager_reuses_learned_underlying_increment() -> None:
    class StubBroker:
        def __init__(self) -> None:
            self.placed_orders: list[dict] = []
            self.preflight_calls = 0

        async def preflight_single_leg(self, payload: dict) -> dict:
            self.preflight_calls += 1
            return {}

        async def place_order(self, payload: dict) -> dict:
            self.placed_orders.append(dict(payload))
            if len(self.placed_orders) == 1:
                raise ValueError("limitPrice must be in increments of $0.05")
            return {"orderId": f"OID{len(self.placed_orders)}"}

        async def close(self) -> None:
            return None

    broker = StubBroker()
    manager = OrderManager(broker=broker)

    first = asyncio.run(manager.place_entry_order("TSLA260410C00260000", 3.21, 1))
    second = asyncio.run(manager.place_entry_order("TSLA260410C00265000", 4.22, 1))

    assert first.order_id == "OID2"
    assert second.order_id == "OID3"
    assert broker.placed_orders[2]["limitPrice"] == "4.25"


def test_cancel_ack_requires_terminal_zero_fill_readback() -> None:
    class StubBroker:
        def __init__(self, payload: dict) -> None:
            self.payload = payload
            self.cancel_calls: list[str] = []

        async def cancel_order(self, order_id: str) -> dict:
            self.cancel_calls.append(order_id)
            return {}  # Public 200: request accepted, not cancellation proof.

        async def get_order(self, order_id: str) -> dict:
            del order_id
            return self.payload

    cases = [
        ({"status": "PENDING_CANCEL", "quantity": "2"}, False, "cancel_pending:PENDING_CANCEL"),
        ({"status": "CANCELLED", "quantity": "2", "filledQuantity": None}, True, None),
        (
            {"status": "CANCELLED", "quantity": "2", "filledQuantity": "1"},
            False,
            "cancel_terminal_with_fill_or_ambiguous_quantity:CANCELLED",
        ),
        (
            {"status": "CANCELLED", "quantity": "2", "filledQuantity": "N/A"},
            False,
            "cancel_terminal_with_fill_or_ambiguous_quantity:CANCELLED",
        ),
    ]

    for payload, expected_ok, expected_error in cases:
        broker = StubBroker(payload)
        manager = OrderManager(broker=broker)

        ok, error = asyncio.run(manager.cancel_order("ORDER123"))

        assert ok is expected_ok
        assert error == expected_error
        assert broker.cancel_calls == ["ORDER123"]


def test_cancel_can_confirm_terminal_order_after_request_error() -> None:
    class StubBroker:
        async def cancel_order(self, order_id: str) -> dict:
            del order_id
            raise ValueError("already closed")

        async def get_order(self, order_id: str) -> dict:
            del order_id
            return {"status": "CANCELLED", "filledQuantity": None}

    ok, error = asyncio.run(OrderManager(broker=StubBroker()).cancel_order("ORDER123"))

    assert ok is True
    assert error is None


def test_get_order_status_classifies_documented_indexing_lag() -> None:
    class StubBroker:
        async def get_order(self, order_id: str) -> dict:
            request = httpx.Request("GET", f"https://api.public.com/order/{order_id}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    status, payload, error = asyncio.run(OrderManager(broker=StubBroker()).get_order_status("ORDER123"))

    assert status is None
    assert payload is None
    assert error == "order_not_indexed_yet"
