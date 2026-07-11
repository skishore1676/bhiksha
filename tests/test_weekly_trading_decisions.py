import asyncio
from datetime import UTC, date, datetime
import json
from pathlib import Path

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.weekly_trading_decisions import (
    render_weekly_trading_decisions_markdown,
    write_weekly_trading_decisions,
)
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteTradeStateRepository


def test_weekly_decision_renderer_names_outcome_and_human_gate() -> None:
    report = {
        "artifact_id": "bhiksha-weekly-trading-decisions:2026-07-10",
        "week_end": date(2026, 7, 10).isoformat(),
        "facts_export_receipt": {"fact_count": 9, "sha256": "abc"},
        "workbook_update": {"status": "ok", "receipt": "ledger:abc"},
        "scorecard": {
            "headline": {
                "live": {"trades": 3, "total_pnl_usd": 125.0},
                "shadow": {"trades": 6, "total_pnl_usd": -20.0},
            },
            "promotion": {"candidates": [], "near_misses": []},
            "data_quality": {},
        },
    }

    markdown = render_weekly_trading_decisions_markdown(report)

    assert markdown.startswith("# Weekly Trading Decisions — Performance, Promotions & Fixes")
    assert "no promotion decision is required" in markdown
    assert "workbook update: `ok`" in markdown
    assert "require Suman's explicit decision" in markdown
    json.dumps(report)


def test_weekly_decision_writer_emits_normalized_fact_receipt(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = SQLiteTradeStateRepository(str(db_path), backend=SQLiteBackend(str(db_path)))

    async def seed() -> None:
        await trades.upsert_trade(TradeRecord(
            trade_id="shadow-1", deployment_id="candidate_shadow_row_1", symbol="QQQ",
            option_symbol="QQQ260713P00560000", quantity=1, entry_price=1.0,
            entry_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            status="open_protected", entry_order_id="SHADOW_ENTRY",
        ))
        await trades.mark_closed(
            "shadow-1", exit_order_id="DRY_RUN", exit_price=1.5,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            exit_order_status="FILLED", exit_order_type="PAPER",
        )

    asyncio.run(seed())
    result = write_weekly_trading_decisions(
        db_path, output_dir=tmp_path / "reports", week_end="2026-07-10",
    )
    export = json.loads(result.facts_path.read_text(encoding="utf-8"))

    assert export["schema"] == "bhiksha.trading_decision_facts.v1"
    assert export["receipt"]["status"] == "ok"
    assert export["receipt"]["fact_count"] == 1
    assert export["facts"][0]["lane"] == "shadow"
    assert export["facts"][0]["realized_pnl_usd"] == 50.0


def test_weekly_publisher_binds_stable_review_id() -> None:
    source = Path("src/bhiksha/tools/launchd_job.py").read_text(encoding="utf-8")

    assert 'review_id=result.report["artifact_id"]' in source
