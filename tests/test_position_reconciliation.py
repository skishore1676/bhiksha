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
            "quantity": "1",
            "filledQuantity": None,
        }
    ]

    tracked = reconcile_public_positions(positions, deployments, orders=orders)

    assert len(tracked) == 1
    assert tracked[0].stop_order_id == "STOP123"


def test_reconcile_keeps_async_cancel_and_replace_orders_attached() -> None:
    deployments = _historical_enabled_deployments()
    positions = [
        {
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "quantity": "1.0",
        }
    ]

    for status in ("PENDING_CANCEL", "PENDING_REPLACE", "QUEUED_CANCELLED"):
        orders = [
            {
                "orderId": f"STOP-{status}",
                "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                "type": "STOP",
                "side": "SELL",
                "status": status,
                "quantity": "1",
                "filledQuantity": None,
            }
        ]

        tracked = reconcile_public_positions(positions, deployments, orders=orders)

        assert tracked[0].stop_order_id == f"STOP-{status}"


def test_reconcile_leaves_mismatched_stop_unattached_for_quantity_repair() -> None:
    deployments = _historical_enabled_deployments()
    positions = [
        {
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "quantity": "2.0",
        }
    ]
    orders = [
        {
            "orderId": "STOP_ONE_LOT",
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "type": "STOP",
            "side": "SELL",
            "status": "NEW",
            "quantity": "1",
            "filledQuantity": None,
        }
    ]

    tracked = reconcile_public_positions(positions, deployments, orders=orders)

    assert tracked[0].quantity == 2
    assert tracked[0].stop_order_id is None


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


def test_reconcile_does_not_treat_stale_trade_record_as_working_protection() -> None:
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
    known_trades = [
        TradeRecord(
            trade_id="TRADE123",
            deployment_id="market_impulse_qqq_short_v1",
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="open_protected",
            entry_order_id="ENTRY123",
            stop_order_id="STALE_STOP",
            stop_price=1.25,
            target_order_id="STALE_TARGET",
            target_price=3.50,
        )
    ]

    tracked = reconcile_public_positions(
        positions,
        deployments,
        orders=[],
        known_trades=known_trades,
    )

    assert tracked[0].trade_id == "TRADE123"
    assert tracked[0].stop_order_id is None
    assert tracked[0].stop_price is None
    assert tracked[0].target_order_id is None
    assert tracked[0].target_price is None


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


def _matched_open_trade_fixture(entry_order_id, status="open_protected"):
    qqq = historical_deployment("market_impulse_qqq_short_v1")
    positions = [
        {
            "instrument": {
                "symbol": "QQQ260401P00556000",
                "type": "OPTION",
            },
            "quantity": "1.0",
            "openedAt": "2026-03-30T14:31:00Z",
            "costBasis": {"unitCost": "2.73"},
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
            status=status,
            entry_order_id=entry_order_id,
        )
    ]
    return positions, [qqq], known_trades


def test_reconcile_matched_open_live_trade_keeps_live_source() -> None:
    """The 2026-07-01 root cause: reconciliation must not strip live identity.

    A broker position matched to a durable OPEN trade record with a REAL broker
    entry order id reconciles as ``live_open`` — so the profile-exit dispatch
    allowlist (which only opens for live_open/live_pending) can stay open for a
    position the reconciliation sweep has replaced.
    """
    positions, deployments, known_trades = _matched_open_trade_fixture("a1b2c3d4-real-order")

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].trade_id == "TRADE123"
    assert tracked[0].source == "live_open"

    from bhiksha.execution.profile_exit import profile_exit_dispatch_allowed

    assert profile_exit_dispatch_allowed(
        live=True,
        deployment_shadow_only=False,
        position_source=tracked[0].source,
        runtime_mode="live_approval_gated",
    )


def test_reconcile_pending_entry_hold_uses_fail_closed_live_source() -> None:
    from bhiksha.state.position_tracker import LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE

    positions, deployments, known_trades = _matched_open_trade_fixture(
        "a1b2c3d4-real-order",
        status="pending_entry_reconcile",
    )

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert tracked[0].source == LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE


def test_reconcile_matched_paper_entry_stays_broker_sync() -> None:
    positions, deployments, known_trades = _matched_open_trade_fixture("SHADOW_ENTRY")

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].source == "broker_sync"


def test_reconcile_matched_trade_without_entry_order_id_stays_broker_sync() -> None:
    positions, deployments, known_trades = _matched_open_trade_fixture(None)

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].source == "broker_sync"


def test_reconcile_matched_closed_trade_stays_broker_sync_even_with_live_entry_id() -> None:
    positions, deployments, known_trades = _matched_open_trade_fixture("a1b2c3d4-real-order", status="closed")

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].source == "broker_sync"


def test_reconcile_stale_open_trade_does_not_capture_new_fill() -> None:
    """Audit repro (2026-07-02): a stale open record (close-write lagged) must
    NOT capture a brand-new fill on the same contract when the broker's own
    evidence (openedAt / cost basis) contradicts it — the position degrades to
    broker_recovered (gate shut) instead of inheriting the stale trade's id,
    live authority, and ladder state."""
    qqq = historical_deployment("market_impulse_qqq_short_v1")
    positions = [
        {
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "quantity": "1.0",
            # New fill: today, at a very different premium.
            "openedAt": "2026-03-31T14:00:00Z",
            "costBasis": {"unitCost": "5.40"},
        }
    ]
    stale_open_trade = TradeRecord(
        trade_id="STALE123",
        deployment_id=qqq.deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.73,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),  # >6h earlier
        status="open_protected",  # should be closed; close-write failed
        entry_order_id="REAL-ORDER-1",
    )

    tracked = reconcile_public_positions(positions, [qqq], known_trades=[stale_open_trade])

    assert len(tracked) == 1
    assert tracked[0].trade_id != "STALE123"
    assert tracked[0].source == "broker_recovered"


def test_reconcile_two_open_trades_same_contract_degrades_safely() -> None:
    """Two plausible open records for the same contract with no distinguishing
    broker evidence: must NOT silently pick one — falls back to
    broker_recovered (single-deployment symbol) with a synthetic id."""
    qqq = historical_deployment("market_impulse_qqq_short_v1")
    positions = [
        {
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "quantity": "1.0",
        }
    ]
    trades = [
        TradeRecord(
            trade_id=f"T{i}",
            deployment_id=qqq.deployment_id,
            symbol="QQQ",
            option_symbol="QQQ260401P00556000",
            quantity=1,
            status="open_protected",
            entry_order_id=f"REAL-{i}",
        )
        for i in (1, 2)
    ]

    tracked = reconcile_public_positions(positions, [qqq], known_trades=trades)

    assert len(tracked) == 1
    assert tracked[0].trade_id not in {"T1", "T2"}
    assert tracked[0].source == "broker_recovered"


def test_reconcile_closed_trade_order_ids_do_not_shadow_open_trade() -> None:
    """A closed trade's historical order id must not win the order-id index
    over an open trade (index is open-trades-only, newest-first)."""
    qqq = historical_deployment("market_impulse_qqq_short_v1")
    positions = [
        {
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "quantity": "1.0",
        }
    ]
    orders = [
        {
            "orderId": "STOP-SHARED",
            "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
            "type": "STOP",
            "side": "SELL",
            "status": "NEW",
        }
    ]
    # newest-first ordering, as get_recent_trades returns (updated_at DESC)
    open_trade = TradeRecord(
        trade_id="OPEN1",
        deployment_id=qqq.deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        status="open_protected",
        entry_order_id="REAL-OPEN",
        stop_order_id="STOP-SHARED",
    )
    closed_trade = TradeRecord(
        trade_id="CLOSED1",
        deployment_id=qqq.deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        status="closed",
        entry_order_id="REAL-CLOSED",
        exit_order_id="STOP-SHARED",
    )

    tracked = reconcile_public_positions(
        positions, [qqq], orders=orders, known_trades=[open_trade, closed_trade]
    )

    assert len(tracked) == 1
    assert tracked[0].trade_id == "OPEN1"
    assert tracked[0].source == "live_open"


def test_reconcile_price_divergence_alone_does_not_reject_true_record() -> None:
    """REGRESSION-D (re-audit 2026-07-02): a true record whose entry_price
    diverges modestly from the broker cost basis (fallback-to-limit record +
    price-improved fill) must still match — price alone contradicts only on
    gross divergence (>10% relative and >$0.25)."""
    positions, deployments, known_trades = _matched_open_trade_fixture("a1b2c3d4-real-order")
    # Record says 2.73 (fixture); broker fill improved to 2.55 (~7%, $0.18).
    positions[0]["costBasis"]["unitCost"] = "2.55"

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].trade_id == "TRADE123"
    assert tracked[0].source == "live_open"


def test_reconcile_gross_price_divergence_still_contradicts() -> None:
    """A genuinely different fill (gross price divergence, no matching
    timestamp evidence) must still be rejected."""
    positions, deployments, known_trades = _matched_open_trade_fixture("a1b2c3d4-real-order")
    positions[0]["costBasis"]["unitCost"] = "5.40"  # ~66% off, $2.67
    positions[0]["openedAt"] = "2026-03-31T14:00:00Z"  # >6h from record

    tracked = reconcile_public_positions(positions, deployments, known_trades=known_trades)

    assert len(tracked) == 1
    assert tracked[0].trade_id != "TRADE123"
    assert tracked[0].source == "broker_recovered"
