import asyncio
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sqlite3

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.weekly_trading_decisions import (
    finalize_weekly_trading_decisions,
    render_weekly_trading_decisions_markdown,
    weekly_stable_digest,
    write_weekly_trading_decisions,
)
from bhiksha.persistence.sqlite import (
    SQLiteBackend,
    SQLiteEventRepository,
    SQLiteTradeStateRepository,
)


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
            "promotion_candidates": {"candidates": [], "near_misses": []},
            "data_quality_warnings": [],
            "lanes": [{
                "deployment_id": "weak-shadow", "display_id": "weak-shadow",
                "mode": "shadow", "closed": 4, "total_pnl_usd": -300.0,
                "avg_return_pct": -12.5,
            }],
        },
    }

    markdown = render_weekly_trading_decisions_markdown(report)

    assert markdown.startswith("# Weekly Trading Decisions — Performance, Promotions & Fixes")
    assert "no promotion decision is required" in markdown
    assert "PERFORMANCE FIX REVIEW" in markdown
    assert "diagnose / keep observing / retire" in markdown
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
        db_path,
        output_dir=tmp_path / "reports",
        week_end="2026-07-10",
        exit_edge_db_path=tmp_path / "missing-exit-edge.db",
        exit_edge_status_path=tmp_path / "missing-exit-edge-status.json",
    )
    export = json.loads(result.facts_path.read_text(encoding="utf-8"))
    governance = json.loads(result.governance_path.read_text(encoding="utf-8"))

    assert export["schema"] == "bhiksha.trading_decision_facts.v1"
    assert export["receipt"]["status"] == "ok"
    assert export["receipt"]["fact_count"] == 1
    assert export["facts"][0]["lane"] == "shadow"
    assert export["facts"][0]["realized_pnl_usd"] == 50.0
    assert governance["schema"] == "bhiksha.trading_governance_evidence.v1"
    assert governance["receipt"]["status"] == "ok"
    assert result.report["governance_evidence"] == str(result.governance_path)
    assert result.exit_edge_path.is_file()
    assert result.report["exit_edge"]["verdict"]["status"] == "not_collecting"
    assert "cumulative paired cohorts: `Unavailable`" in (
        result.markdown_path.read_text(encoding="utf-8")
    )
    assert (
        result.report["exit_edge_evidence_receipt"]["sha256"]
        == json.loads(result.exit_edge_path.read_text(encoding="utf-8"))["receipt"][
            "sha256"
        ]
    )

    finalized = finalize_weekly_trading_decisions(
        result,
        {"status": "ok", "receipt": "ledger:test"},
    )
    assert finalized.report["receipt"]["status"] == "ok"
    assert (
        finalized.report["receipt"]["sha256"]
        == weekly_stable_digest(finalized.report)
    )
    finalized.report["scorecard"]["headline"]["live"]["total_pnl_usd"] = 999_999
    assert weekly_stable_digest(finalized.report) != finalized.report["receipt"]["sha256"]

    rerun = write_weekly_trading_decisions(
        db_path, output_dir=tmp_path / "reports", week_end="2026-07-10",
    )
    rerun_export = json.loads(rerun.facts_path.read_text(encoding="utf-8"))
    assert rerun_export["receipt"]["sha256"] == export["receipt"]["sha256"]


def test_weekly_export_reclassifies_historical_shadow_degradation(
    tmp_path,
) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)
    reports = tmp_path / "reports"
    reports.mkdir()

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="shadow-noise",
                deployment_id="qqq-shadow",
                symbol="QQQ",
                option_symbol="QQQ260727P00500000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(
                    2026,
                    7,
                    27,
                    14,
                    0,
                    tzinfo=UTC,
                ),
                status="open_unprotected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await events.append(
            "runtime_issue",
            {
                "category": "exit_state_degraded_protection",
                "trade_id": "shadow-noise",
                "symbol": "QQQ",
            },
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET created_at='2026-07-27T15:00:00+00:00'"
        )
        conn.commit()
    (reports / "trade_session_report_2026-07-27.json").write_text(
        json.dumps(
            {
                "trading_date": "2026-07-27",
                "event_type_counts": {"runtime_issue": 16_416},
                "provider_health": {
                    "reconciliation": {},
                    "runtime_issue_counts": {
                        "exit_state_degraded_protection": 16_416
                    },
                },
                "status": {"level": "YELLOW"},
            }
        ),
        encoding="utf-8",
    )

    result = write_weekly_trading_decisions(
        db_path,
        output_dir=reports,
        week_end="2026-07-31",
        exit_edge_db_path=tmp_path / "missing-exit-edge.db",
        exit_edge_status_path=tmp_path / "missing-exit-edge-status.json",
    )
    export = json.loads(result.facts_path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in export["daily_status"]
        if item["date"] == "2026-07-27"
    )

    assert row["operational_issue_count"] == 0
    assert row["shadow_diagnostic_count"] == 1


def test_weekly_publisher_binds_stable_review_id() -> None:
    source = Path("src/bhiksha/tools/launchd_job.py").read_text(encoding="utf-8")

    assert 'review_id=result.report["artifact_id"]' in source


def test_weekly_job_defaults_to_evidence_only() -> None:
    source = Path("src/bhiksha/tools/launchd_job.py").read_text(encoding="utf-8")

    assert 'os.getenv("BHIKSHA_WEEKLY_REVIEW_MODE", "off")' in source
