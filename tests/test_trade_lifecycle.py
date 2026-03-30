from bhiksha.state.lifecycle import LifecycleState, TradeLifecycleStore
from bhiksha.state.position_tracker import TrackedPosition


def test_trade_lifecycle_store_blocks_entry_when_trade_is_active() -> None:
    store = TradeLifecycleStore()

    assert store.can_submit_entry("QQQ", "market_impulse_qqq_short_v1") is True

    store.begin_entry("QQQ", "market_impulse_qqq_short_v1", option_symbol="QQQ260401P00556000", order_id="ENTRY123")
    assert store.can_submit_entry("QQQ", "market_impulse_qqq_short_v1") is False

    store.mark_closed("QQQ", "market_impulse_qqq_short_v1")
    assert store.can_submit_entry("QQQ", "market_impulse_qqq_short_v1") is True


def test_trade_lifecycle_store_syncs_from_reconciled_positions() -> None:
    store = TradeLifecycleStore()
    positions = [
        TrackedPosition(
            symbol="QQQ",
            deployment_id="market_impulse_qqq_short_v1",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            stop_order_id="STOP123",
            stop_price=1.1,
        ),
        TrackedPosition(
            symbol="SPY",
            deployment_id="market_impulse_spy_short_v1",
            option_symbol="SPY260401P00630000",
            quantity=1,
            target_order_id="TARGET123",
            target_price=2.5,
        ),
    ]

    store.sync_from_positions(positions)

    qqq = store.get("QQQ", "market_impulse_qqq_short_v1")
    spy = store.get("SPY", "market_impulse_spy_short_v1")

    assert qqq is not None
    assert qqq.state == LifecycleState.OPEN_PROTECTED
    assert spy is not None
    assert spy.state == LifecycleState.TARGET_ACTIVE
