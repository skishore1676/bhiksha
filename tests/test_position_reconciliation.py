from bhiksha.config.loader import load_deployments
from bhiksha.domain.models import TradeRecord
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


def test_reconcile_public_positions_prefers_known_trade_identity_over_symbol_match() -> None:
    deployments = load_deployments("config/deployments")
    qqq = next(d for d in deployments if d.deployment_id == "market_impulse_qqq_short_v1")
    sibling = qqq.model_copy(update={"deployment_id": "market_impulse_qqq_short_v2"})
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
    known_trades = [
        TradeRecord(
            trade_id="TRADE123",
            deployment_id=sibling.deployment_id,
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="open_protected",
            entry_order_id="TRADE123",
            stop_order_id="STOP123",
        )
    ]

    tracked = reconcile_public_positions(positions, [qqq, sibling], orders=orders, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].deployment_id == "market_impulse_qqq_short_v2"
    assert tracked[0].trade_id == "TRADE123"


def test_reconcile_public_positions_skips_ambiguous_same_contract_trade_identity() -> None:
    deployments = load_deployments("config/deployments")
    qqq = next(d for d in deployments if d.deployment_id == "market_impulse_qqq_short_v1")
    sibling = qqq.model_copy(update={"deployment_id": "market_impulse_qqq_short_v2"})
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
        }
    ]
    known_trades = [
        TradeRecord(
            trade_id="TRADE123",
            deployment_id=qqq.deployment_id,
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="open_protected",
        ),
        TradeRecord(
            trade_id="TRADE456",
            deployment_id=sibling.deployment_id,
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="open_protected",
        ),
    ]

    tracked = reconcile_public_positions(positions, [qqq, sibling], known_trades=known_trades)

    assert tracked == []
