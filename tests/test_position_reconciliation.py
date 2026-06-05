from datetime import UTC, datetime

from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import TradeRecord
from bhiksha.state.reconciliation import reconcile_public_positions
from historical_config import historical_deployment, load_historical_deployments


def _historical_enabled_deployments():
    ids = {"market_impulse_qqq_short_v1", "market_impulse_spy_short_v1"}
    return [
        deployment.model_copy(update={"enabled": True})
        for deployment in load_historical_deployments()
        if deployment.deployment_id in ids
    ]


def test_reconcile_public_positions_maps_option_positions_to_deployments() -> None:
    deployments = _historical_enabled_deployments()
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
    deployments = _historical_enabled_deployments()
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
    deployments = _historical_enabled_deployments()
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
    deployments = load_historical_deployments()
    qqq = historical_deployment("market_impulse_qqq_short_v1")
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


def test_reconcile_public_positions_maps_live_limit_as_exit_when_trade_is_exit_pending() -> None:
    deployments = _historical_enabled_deployments()
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
            "orderId": "EXIT123",
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "type": "LIMIT",
            "side": "SELL",
            "status": "NEW",
            "limitPrice": "2.70",
        }
    ]
    known_trades = [
        TradeRecord(
            trade_id="TRADE123",
            deployment_id="market_impulse_qqq_short_v1",
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="exit_pending",
            entry_order_id="ENTRY123",
            exit_order_id="EXIT123",
            exit_limit_price=2.70,
            exit_mode=ExitMode.STRATEGY,
        )
    ]

    tracked = reconcile_public_positions(positions, deployments, orders=orders, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].exit_order_id == "EXIT123"
    assert tracked[0].exit_limit_price == 2.70
    assert tracked[0].target_order_id is None


def test_reconcile_public_positions_skips_ambiguous_same_contract_trade_identity() -> None:
    deployments = load_historical_deployments()
    qqq = historical_deployment("market_impulse_qqq_short_v1")
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


def test_reconcile_public_positions_matches_recent_closed_trade_by_opened_at_and_price() -> None:
    deployments = load_historical_deployments()
    qqq = historical_deployment("market_impulse_qqq_short_v1")
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
            "openedAt": "2026-03-30T14:31:00Z",
            "costBasis": {
                "unitCost": "2.73",
            },
        }
    ]
    known_trades = [
        TradeRecord(
            trade_id="TRADE123",
            deployment_id=qqq.deployment_id,
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            entry_price=2.73,
            entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
            status="closed",
            entry_order_id="ENTRY123",
        )
    ]

    tracked = reconcile_public_positions(positions, [qqq], known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].trade_id == "TRADE123"
    assert tracked[0].source == "broker_sync"


def test_reconcile_public_positions_creates_synthetic_trade_for_orphan() -> None:
    deployments = _historical_enabled_deployments()
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
            "openedAt": "2026-03-30T14:31:00Z",
            "costBasis": {
                "unitCost": "2.73",
            },
        }
    ]

    tracked = reconcile_public_positions(positions, deployments, known_trades=[])

    assert len(tracked) == 1
    assert tracked[0].trade_id is not None
    assert tracked[0].trade_id.startswith("recovered:")
    assert tracked[0].source == "broker_recovered"
