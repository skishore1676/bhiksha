from bhiksha.config.loader import load_deployments
from bhiksha.state.reconciliation import reconcile_public_positions


def test_reconcile_public_positions_maps_option_positions_to_deployments() -> None:
    deployments = load_deployments("config/deployments")
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260330P00558000-OPTION",
                "type": "OPTION",
            },
            "quantity": "1.0",
        },
        {
            "instrument": {
                "symbol": "SPY260330P00548000",
                "type": "OPTION",
            },
            "quantity": "2.0",
        },
    ]

    tracked = reconcile_public_positions(positions, deployments)

    assert len(tracked) == 2
    assert tracked[0].symbol == "QQQ"
    assert tracked[0].option_symbol == "QQQ260330P00558000"
    assert tracked[1].symbol == "SPY"
    assert tracked[1].quantity == 2


def test_reconcile_public_positions_attaches_open_stop_order() -> None:
    deployments = load_deployments("config/deployments")
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
        }
    ]
    orders = [
        {
            "orderId": "STOP123",
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "type": "STOP",
            "side": "SELL",
            "status": "NEW",
        }
    ]

    tracked = reconcile_public_positions(positions, deployments, orders=orders)

    assert len(tracked) == 1
    assert tracked[0].stop_order_id == "STOP123"


def test_reconcile_public_positions_attaches_entry_and_target_metadata() -> None:
    deployments = load_deployments("config/deployments")
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
            "costBasis": {
                "unitCost": "2.73",
            },
        }
    ]
    orders = [
        {
            "orderId": "TARGET123",
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "type": "LIMIT",
            "side": "SELL",
            "status": "NEW",
            "limitPrice": "3.35",
        }
    ]

    tracked = reconcile_public_positions(positions, deployments, orders=orders)

    assert len(tracked) == 1
    assert tracked[0].entry_price == 2.73
    assert tracked[0].target_order_id == "TARGET123"
    assert tracked[0].target_price == 3.35
