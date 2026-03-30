import asyncio

from bhiksha.ops.summary import build_session_summary
from bhiksha.persistence.sqlite import SQLiteEventRepository


def test_session_summary_aggregates_lifecycle_and_trade_events(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))

    async def seed():
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "previous_state": None,
                "new_state": "pending_entry",
                "reason": "entry_submitted",
            },
        )
        await repo.append(
            "trade_plan",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "option_symbol": "QQQ260401P00556000",
            },
        )
        await repo.append(
            "lifecycle_transition",
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "symbol": "QQQ",
                "previous_state": "pending_entry",
                "new_state": "open_protected",
                "reason": "entry_filled_open_protected",
            },
        )

    asyncio.run(seed())

    summary = build_session_summary(str(db_path), recent_limit=5)

    assert summary.total_events == 3
    assert summary.event_type_counts["lifecycle_transition"] == 2
    assert summary.deployment_event_counts["market_impulse_qqq_short_v1"] == 3
    assert summary.lifecycle_last_state["market_impulse_qqq_short_v1"] == "open_protected"
    assert summary.recent_events[-1].detail == "pending_entry->open_protected (entry_filled_open_protected)"
