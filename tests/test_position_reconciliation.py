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
