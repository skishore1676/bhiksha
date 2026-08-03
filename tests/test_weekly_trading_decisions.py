import asyncio
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sqlite3

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.weekly_trading_decisions import (
    _decision_evidence_status,
    _load_option_snapshot_selected_matches,
    build_trading_decision_export,
    finalize_weekly_trading_decisions,
    render_weekly_trading_decisions_markdown,
    weekly_stable_digest,
    write_weekly_trading_decisions,
)
from bhiksha.ops.weekly_scorecard import build_weekly_scorecard
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


def test_evidence_status_quarantines_snapshot_without_selected_contract() -> None:
    row = {
        "evidence_packet_id": "a" * 64,
        "experiment_id": "experiment-v1",
        "cohort_id": "cohort-v1",
        "cohort_contract_sha256": "b" * 64,
        "deployment_contract_sha256": "c" * 64,
        "declared_option_selection_contract_sha256": "d" * 64,
        "exit_policy_sha256": "e" * 64,
        "plan_revision_id": "sha256:plan",
        "session_id": "session-v1",
        "fact_receipt_id": "sha256:fact",
        "option_selection_snapshot_id": "snapshot-v1",
        "option_selection_snapshot_persisted": 1,
        "option_candidate_set_sha256": "f" * 64,
        "actual_option_selection_sha256": "1" * 64,
    }

    status, issues = _decision_evidence_status(
        row,
        {"exit_attribution": "profile:no_progress"},
        option_snapshot_selected_match=False,
    )

    assert status == "plumbing_invalid"
    assert issues == ["option_selection_selected_contract_not_persisted"]


def test_snapshot_consistency_requires_attempt_and_selected_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE option_chain_snapshot_attempts (
            snapshot_id TEXT PRIMARY KEY,
            selected_option_symbol TEXT
        );
        CREATE TABLE option_chain_snapshots (
            snapshot_id TEXT,
            option_symbol TEXT,
            is_selected INTEGER
        );
        INSERT INTO option_chain_snapshot_attempts VALUES
            ('good-snapshot', 'GOOD'),
            ('missing-winner', 'FARTHER');
        INSERT INTO option_chain_snapshots VALUES
            ('good-snapshot', 'GOOD', 1),
            ('missing-winner', 'NEAREST', 0);
        CREATE TABLE trades (
            trade_id TEXT,
            option_symbol TEXT,
            option_selection_snapshot_id TEXT
        );
        INSERT INTO trades VALUES
            ('good-trade', 'GOOD', 'good-snapshot'),
            ('bad-trade', 'FARTHER', 'missing-winner');
        """
    )
    rows = conn.execute("SELECT * FROM trades ORDER BY trade_id").fetchall()

    matches = _load_option_snapshot_selected_matches(conn, rows)

    assert matches == {"bad-trade": False, "good-trade": True}


def test_weekly_decision_writer_emits_normalized_fact_receipt(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    trades = SQLiteTradeStateRepository(str(db_path), backend=SQLiteBackend(str(db_path)))

    async def seed() -> None:
        await trades.upsert_trade(TradeRecord(
            trade_id="shadow-1", deployment_id="candidate_shadow_row_1", symbol="QQQ",
            option_symbol="QQQ260713P00560000", quantity=1, entry_price=1.0,
            entry_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            status="open_protected", entry_order_id="SHADOW_ENTRY",
            active_plan_id="plan-shadow",
            plan_revision_id="sha256:plan-shadow",
            session_id="plan-shadow:session",
            research_run_id="run-shadow",
            evidence_packet_id="a" * 64,
            evidence_artifact_sha256="b" * 64,
            evidence_artifact_uri="mala-evidence://shadow",
            canary_id="shadow-v1",
            canary_authorization_sha256="c" * 64,
            fact_receipt_id="sha256:fact-shadow",
        ))
        await trades.mark_closed(
            "shadow-1", exit_order_id="DRY_RUN", exit_price=1.5,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            exit_order_status="FILLED", exit_order_type="PAPER",
            exit_rule="strategy:paper_close",
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
    assert export["facts"][0]["plan_revision_id"] == "sha256:plan-shadow"
    assert export["facts"][0]["session_id"] == "plan-shadow:session"
    assert export["facts"][0]["fact_receipt_id"] == "sha256:fact-shadow"
    assert export["facts"][0]["evidence_packet_id"] == "a" * 64
    assert governance["schema"] == "bhiksha.trading_governance_evidence.v1"
    assert governance["receipt"]["status"] == "ok"
    assert result.report["governance_evidence"] == str(result.governance_path)
    assert result.exit_edge_path.is_file()
    assert result.report["exit_edge"]["verdict"]["status"] == "not_collecting"
    assert result.report["exit_policy_evidence"] == {
        "bhiksha": str(result.exit_edge_path)
    }
    assert (
        result.report["exit_policy_evidence_receipts"]["bhiksha"]["sha256"]
        == json.loads(result.exit_edge_path.read_text(encoding="utf-8"))["receipt"][
            "sha256"
        ]
    )
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


def test_weekly_export_separates_terminal_no_fill_and_unknown_exit_observations(
    tmp_path,
) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="FILLED-CLOSED",
                deployment_id="filled_lane",
                symbol="QQQ",
                option_symbol="QQQ260713P00560000",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY-FILLED",
            )
        )
        await trades.mark_closed(
            "FILLED-CLOSED",
            exit_order_id="EXIT-FILLED",
            exit_price=1.5,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            exit_order_status="FILLED",
            exit_order_type="LIMIT",
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="AMD-UNFILLED",
                deployment_id="amd_live",
                symbol="AMD",
                option_symbol="AMD260717C00260000",
                quantity=1,
                entry_price=6.60,
                entry_timestamp=datetime(2026, 7, 10, 14, 1, tzinfo=UTC),
                status="pending_entry",
                entry_order_id="ENTRY-AMD-UNFILLED",
            )
        )
        await trades.mark_closed("AMD-UNFILLED")
        await trades.upsert_trade(
            TradeRecord(
                trade_id="FILLED-EXIT-MISSING",
                deployment_id="missing_exit_lane",
                symbol="NVDA",
                option_symbol="NVDA260717C00200000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 10, 14, 2, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY-NVDA",
                stop_order_id="STOP-NVDA",
            )
        )
        await trades.mark_closed("FILLED-EXIT-MISSING")

        await events.append(
            "entry_reconcile_released",
            {
                "deployment_id": "amd_live",
                "trade_id": "AMD-UNFILLED",
                "order_id": "ENTRY-AMD-UNFILLED",
                "status": "CANCELED",
                "payload": {"status": "CANCELED", "filledQuantity": None},
            },
        )
        await events.append(
            "entry_reconcile_released",
            {
                "deployment_id": "no_fill_lane",
                "trade_id": "REJECTED-NO-FILL",
                "order_id": "ENTRY-REJECTED",
                "status": "REJECTED",
                "payload": {"status": "REJECTED", "filledQuantity": 0},
            },
        )
        await events.append(
            "signal_decision",
            {
                "deployment_id": "no_signal_lane",
                "symbol": "IWM",
                "signal": False,
            },
        )
        await events.append(
            "signal_decision",
            {
                "deployment_id": "blocked_lane",
                "symbol": "META",
                "signal": True,
            },
        )
        await events.append(
            "lifecycle_entry_blocked",
            {
                "deployment_id": "blocked_lane",
                "symbol": "META",
                "state": "open_protected",
            },
        )
        await events.append(
            "trade_plan",
            {
                "trade_id": "PLAN-WITHOUT-RECEIPT",
                "deployment_id": "missing_plan_lane",
                "symbol": "PDD",
                "order_id": "ENTRY-PDD-MISSING",
                "risk_reasons": ["approved"],
            },
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at='2026-07-10T14:05:00+00:00'")
        conn.commit()

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    export = build_trading_decision_export(
        db_path,
        through=date(2026, 7, 10),
        deployments=None,
        report_dir=report_dir,
    )

    assert export["receipt"]["fact_count"] == 1
    assert export["facts"][0]["trade_id"] == "FILLED-CLOSED"
    assert export["facts"][0]["observation_outcome"] == "FILLED/CLOSED"
    assert export["facts"][0]["pnl_eligible"] is True
    outcomes = {
        (row.get("trade_id"), row["observation_outcome"])
        for row in export["observations"]
    }
    assert ("AMD-UNFILLED", "ENTRY_CANCELLED_UNFILLED") in outcomes
    assert ("FILLED-EXIT-MISSING", "MISSING") in outcomes
    assert ("REJECTED-NO-FILL", "NO_FILL") in outcomes
    assert (None, "NO_SIGNAL") in outcomes
    assert (None, "BLOCKED") in outcomes
    assert ("PLAN-WITHOUT-RECEIPT", "MISSING") in outcomes
    amd = next(
        row
        for row in export["observations"]
        if row.get("trade_id") == "AMD-UNFILLED"
    )
    assert amd["pnl_eligible"] is False
    assert amd["source_receipt"].startswith("bhiksha.db#events/")
    assert export["receipt"]["observation_count"] == 6

    scorecard = build_weekly_scorecard(
        db_path,
        week_start="2026-07-06",
        week_end="2026-07-10",
    )
    assert scorecard["headline"]["live"]["closed"] == 2
    assert scorecard["headline"]["live"]["missing_pnl_count"] == 1
    assert scorecard["headline"]["live"]["total_pnl_usd"] is None
    assert {
        row["trade_id"]: row["observation_outcome"]
        for row in scorecard["observation_outcomes"]
    } == {
        "AMD-UNFILLED": "ENTRY_CANCELLED_UNFILLED",
        "FILLED-CLOSED": "FILLED/CLOSED",
        "FILLED-EXIT-MISSING": "MISSING",
    }


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
