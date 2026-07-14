import asyncio
from datetime import UTC, datetime
import json
import sqlite3

from bhiksha.config.models import (
    DeploymentManifest,
    ExecutionSpec,
    RiskSpec,
    SourceSpec,
    StrategySpec,
)
from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import CashBudgetDay, PartialFillRecord, TradeRecord
from bhiksha.ops.daily_report import (
    build_daily_report,
    render_daily_report_markdown,
    render_daily_report_telegram_summary,
    write_daily_report,
)
from bhiksha.persistence.sqlite import (
    SQLiteBackend,
    SQLiteCashBudgetRepository,
    SQLiteEventRepository,
    SQLiteTradeStateRepository,
)


def _minimal_deployment(deployment_id: str, symbol: str, *, metadata: dict | None = None) -> DeploymentManifest:
    return DeploymentManifest(
        deployment_id=deployment_id,
        enabled=True,
        symbol=symbol,
        strategy=StrategySpec(key="market_impulse", params={"direction": "short"}),
        execution=ExecutionSpec(profile="default"),
        risk=RiskSpec(profile="default"),
        source=SourceSpec(metadata=metadata or {}),
    )


def test_daily_report_summarizes_trades_provider_health_and_data_quality(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await events.append(
            "reconciliation_health",
            {
                "symbol": "ALL",
                "stage": "reconciliation",
                "severity": "warning",
                "reason": "periodic",
                "error": "portfolio 400",
            },
        )
        await events.append(
            "target_approach_detected",
            {
                "deployment_id": "mu_shadow",
                "symbol": "ACME",
                "option_symbol": "ACME260612C01200000",
                "target_price": 35.073,
            },
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="trade-mu",
                deployment_id="mu_shadow",
                symbol="ACME",
                option_symbol="ACME260612C01200000",
                quantity=1,
                entry_price=25.98,
                underlying_entry_price=1064.485,
                entry_timestamp=datetime(2026, 6, 3, 13, 39, tzinfo=UTC),
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.mark_closed(
            "trade-mu",
            exit_order_id="DRY_RUN_CLOSE",
            exit_price=29.3,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 3, 19, 56, tzinfo=UTC),
            exit_order_status="FILLED",
            exit_order_type="PAPER",
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-06-03T13:40:00+00:00'")
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-06-03")

    assert report["trade_summary"]["shadow_count"] == 1
    assert report["trade_summary"]["shadow_open_count"] == 0
    assert report["trade_summary"]["shadow_realized_pnl_usd"] == 332.0
    assert report["provider_health"]["reconciliation"]["warning_count"] == 1
    assert report["lifecycle"]["target_approach_detected"] == 1
    assert report["data_quality_warnings"] == []
    assert report["status"] == {"level": "YELLOW", "reason": "provider_warning"}


def test_daily_report_marks_closed_live_trade_with_missing_exit_truth_unknown(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="NVDA-RACE",
                deployment_id="nvda_live",
                symbol="NVDA",
                option_symbol="NVDA260722P00200000",
                quantity=8,
                entry_price=2.18,
                underlying_entry_price=207.59,
                entry_timestamp=datetime(2026, 7, 13, 13, 52, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY-NVDA-RACE",
                stop_order_id="STOP-NVDA-RACE",
                stop_price=1.42,
            )
        )
        await trades.mark_closed("NVDA-RACE", exit_rule="no_progress")

    asyncio.run(seed())
    report = build_daily_report(db_path, trading_date="2026-07-13")

    assert report["trade_summary"]["live_realized_pnl_usd"] is None
    assert report["trade_summary"]["live_missing_exit_truth_count"] == 1
    assert report["status"] == {"level": "YELLOW", "reason": "data_quality_warning"}
    assert report["data_quality_warnings"][0]["category"] == "live_exit_truth_missing"
    markdown = render_daily_report_markdown(report)
    telegram = render_daily_report_telegram_summary(report)
    assert "live realized P&L: `unknown (1 missing exit fill)`" in markdown
    assert "live unknown (1 missing exit fill)" in telegram
    assert "P&L unknown" in telegram


def test_daily_report_includes_banked_partial_fill_pnl(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(TradeRecord(
            trade_id="ladder", deployment_id="qqq_live", symbol="QQQ",
            option_symbol="QQQ260713P00560000", quantity=4, entry_price=1.94,
            entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
            status="target_active", entry_order_id="LIVE_ENTRY", can_ladder=True,
        ))
        await trades.record_partial_fill(PartialFillRecord(
            id=None, trade_id="ladder", deployment_id="qqq_live", symbol="QQQ",
            option_symbol="QQQ260713P00560000", closed_quantity=6,
            order_id="PARTIAL", exit_rule="target_1_partial",
            submitted_at=datetime(2026, 7, 9, 14, 15, tzinfo=UTC),
            fill_price=2.72, fill_quantity=6,
            filled_at=datetime(2026, 7, 9, 14, 15, tzinfo=UTC),
            order_status="FILLED", order_type="MARKET", origin="partial_scale",
        ))
        await trades.mark_closed(
            "ladder", exit_order_id="RUNNER", exit_price=3.43,
            exit_filled_quantity=4,
            exit_filled_at=datetime(2026, 7, 9, 14, 30, tzinfo=UTC),
            exit_order_status="FILLED", exit_order_type="LIMIT",
            exit_rule="target_2_runner",
        )

    asyncio.run(seed())
    report = build_daily_report(db_path, trading_date="2026-07-09")

    assert report["trade_summary"]["live_realized_pnl_usd"] == 1064.0
    assert report["trades"][0]["banked_partial_pnl_usd"] == 468.0
    assert report["trades"][0]["banked_quantity"] == 6


def test_daily_report_flags_only_implausible_underlying_to_strike_ratio(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="normal-high-price",
                deployment_id="meta_shadow",
                symbol="META",
                option_symbol="META260612C00580000",
                quantity=1,
                entry_price=1.0,
                underlying_entry_price=585.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 39, tzinfo=UTC),
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="bad-scale",
                deployment_id="bad_scale_shadow",
                symbol="ACME",
                option_symbol="ACME260612C00012000",
                quantity=1,
                entry_price=1.0,
                underlying_entry_price=585.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 40, tzinfo=UTC),
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )

    asyncio.run(seed())

    report = build_daily_report(db_path, trading_date="2026-06-03")

    assert [warning["trade_id"] for warning in report["data_quality_warnings"]] == ["bad-scale"]
    assert report["data_quality_warnings"][0]["message"].startswith("underlying entry price is far from option strike")
    assert report["status"] == {"level": "YELLOW", "reason": "data_quality_warning"}


def test_daily_report_writes_json_and_markdown(tmp_path) -> None:
    db_path = tmp_path / "missing.db"
    output_dir = tmp_path / "reports"

    result = write_daily_report(db_path, output_dir=output_dir, trading_date="2026-06-03")

    assert result.json_path.exists()
    assert result.markdown_path.exists()
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["trading_date"] == "2026-06-03"
    assert "# Bhiksha Trade Session - 2026-06-03" in result.markdown_path.read_text(encoding="utf-8")


def test_daily_report_surfaces_open_positions_and_protection(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-open",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00480000",
                quantity=2,
                entry_price=3.8,
                entry_timestamp=datetime(2026, 6, 3, 14, 35, tzinfo=UTC),
                status="open_protected",
                entry_order_id="LIVE_ENTRY",
                stop_order_id="STOP1",
                stop_price=2.47,
                target_order_id="TARGET1",
                target_price=5.7,
            )
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="shadow-open",
                deployment_id="mu_shadow",
                symbol="ACME",
                option_symbol="ACME260612C01200000",
                quantity=1,
                entry_price=25.98,
                entry_timestamp=datetime(2026, 6, 3, 14, 36, tzinfo=UTC),
                status="open_unprotected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-target",
                deployment_id="iwm_live",
                symbol="IWM",
                option_symbol="IWM260612C00303000",
                quantity=10,
                entry_price=0.95,
                entry_timestamp=datetime(2026, 6, 3, 14, 37, tzinfo=UTC),
                status="target_active",
                entry_order_id="LIVE_ENTRY2",
                stop_price=0.62,
                target_order_id="TARGET2",
                target_price=1.28,
            )
        )

    asyncio.run(seed())

    report = build_daily_report(db_path, trading_date="2026-06-03")
    body = render_daily_report_telegram_summary(report)

    assert report["trade_summary"]["live_open_count"] == 2
    assert report["trade_summary"]["shadow_open_count"] == 1
    assert report["open_position_summary"] == {
        "protected_count": 1,
        "target_active_count": 1,
        "unprotected_count": 1,
        "exit_pending_count": 0,
    }
    assert report["open_positions"][0]["protection_state"] == "protected"
    iwm_position = next(item for item in report["open_positions"] if item["symbol"] == "IWM")
    assert iwm_position["protection_state"] == "target_active"
    assert "- Open: live 2, shadow 1" in body
    assert "- Protection: protected 1, target active 1, unprotected 1, exit pending 0" in body
    assert "- LIVE QQQ qty 2: entry 3.80, stop 2.47, target 5.70, protected" in body


# --------------------------------------------------------------------------- #
# Item D (2026-07-08 hygiene batch): ladder-capability tag (can_ladder =
# quantity >= 2 at live entry time) surfaced in the trades/open-positions
# tables so the month's analytics can separate full-DNA trades from
# T1-only (no-ladder) trades.
# --------------------------------------------------------------------------- #


def test_daily_report_flags_no_ladder_trades_in_qty_column(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        # 1-lot live entry: cannot express the T1 60/40 ladder.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="amd-no-ladder",
                deployment_id="amd_short_live",
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=1,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 6, 3, 13, 45, tzinfo=UTC),
                status="open_protected",
                stop_order_id="STOP_AMD",
                can_ladder=False,
            )
        )
        # 2-lot live entry: full ladder DNA available.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="qqq-full-dna",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00480000",
                quantity=2,
                entry_price=2.50,
                entry_timestamp=datetime(2026, 6, 3, 13, 46, tzinfo=UTC),
                status="open_protected",
                stop_order_id="STOP_QQQ",
                can_ladder=True,
            )
        )
        # Pre-migration row: can_ladder never recorded -> must render as a
        # bare quantity, never misrepresented as no-ladder.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="legacy-unknown",
                deployment_id="spy_live",
                symbol="SPY",
                option_symbol="SPY260612C00600000",
                quantity=1,
                entry_price=1.20,
                entry_timestamp=datetime(2026, 6, 3, 13, 47, tzinfo=UTC),
                status="open_protected",
                stop_order_id="STOP_SPY",
            )
        )

    asyncio.run(seed())

    report = build_daily_report(db_path, trading_date="2026-06-03")
    by_id = {trade["trade_id"]: trade for trade in report["trades"]}
    assert by_id["amd-no-ladder"]["qty_label"] == "1 (no-ladder)"
    assert by_id["qqq-full-dna"]["qty_label"] == "2"
    assert by_id["legacy-unknown"]["qty_label"] == "1"

    markdown = write_daily_report(db_path, output_dir=tmp_path / "reports", trading_date="2026-06-03").markdown_path.read_text(
        encoding="utf-8"
    )
    assert "| live | AMD | AMD260713P00500000 | 1 (no-ladder) |" in markdown
    assert "| live | QQQ | QQQ260612C00480000 | 2 |" in markdown
    assert "| live | SPY | SPY260612C00600000 | 1 |" in markdown


def test_daily_report_renders_concise_telegram_summary(tmp_path) -> None:
    report = {
        "trading_date": "2026-06-03",
        "status": {"level": "YELLOW", "reason": "data_quality_warning"},
        "trade_summary": {
            "live_count": 0,
            "shadow_count": 2,
            "live_realized_pnl_usd": 0.0,
            "shadow_realized_pnl_usd": 412.0,
        },
        "provider_health": {
            "reconciliation": {"warning_count": 0, "degraded_count": 0, "blocking_count": 0}
        },
        "data_quality_warnings": [
            {"symbol": "ACME", "message": "single-name equity has index-like underlying and strike levels"}
        ],
        "trades": [
            {
                "lane": "shadow",
                "symbol": "ACME",
                "option_symbol": "ACME260612C01200000",
                "quantity": 1,
                "entry_price": 25.98,
                "exit_price": 29.3,
                "realized_pnl_usd": 332.0,
            }
        ],
    }

    body = render_daily_report_telegram_summary(report, markdown_path=tmp_path / "report.md")

    assert "Bhiksha Session Report - 2026-06-03" in body
    assert "Quick read" in body
    assert "- Open: live 0, shadow 0" in body
    assert "- Protection: protected 0, target active 0, unprotected 0, exit pending 0" in body
    assert "- P&L: live $0.00 (0 trades), shadow $412.00 (2 trades)" in body
    assert "Open positions\n- None" in body
    assert "Watch" in body
    assert "- Data quality: 1 warning(s); first=ACME" in body
    assert str(tmp_path / "report.md") in body
    assert len(body.splitlines()) <= 18


def test_report_status_escalates_dead_lane_to_red() -> None:
    from bhiksha.ops.daily_report import _report_status

    status = _report_status(
        provider_events={"blocking_count": 0, "degraded_count": 0, "warning_count": 0},
        data_quality_warnings=[],
        runtime_issue_counts={"dead_lane": 1, "entry_selector_empty": 4},
    )
    assert status == {"level": "RED", "reason": "dead_live_lane"}

    ok_status = _report_status(
        provider_events={"blocking_count": 0, "degraded_count": 0, "warning_count": 0},
        data_quality_warnings=[],
        runtime_issue_counts={},
    )
    assert ok_status == {"level": "GREEN", "reason": "ok"}


def test_report_status_escalates_live_unprotected_position_to_red() -> None:
    from bhiksha.ops.daily_report import _report_status

    status = _report_status(
        provider_events={"blocking_count": 0, "degraded_count": 0, "warning_count": 0},
        data_quality_warnings=[],
        runtime_issue_counts={},
        open_positions=[{"lane": "live", "symbol": "IWM", "protection_state": "unprotected"}],
    )

    assert status == {"level": "RED", "reason": "live_open_unprotected"}


# --------------------------------------------------------------------------- #
# workplan #10: distinct exit attribution (profile:<rule> vs strategy/stop/
# target/hard_flat) surfaced in the Trades table + a compact summary line.
# --------------------------------------------------------------------------- #


def test_daily_report_exit_column_attributes_profile_stop_target_and_hard_flat(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        # A profile-dispatched exit: exit_rule is the attribution source of truth,
        # independent of exit_mode (a profile close via _handle_exit_locked's
        # dry_run branch never sets exit_mode at all).
        await trades.upsert_trade(
            TradeRecord(
                trade_id="profile-exit",
                deployment_id="qqq_profile",
                symbol="QQQ",
                option_symbol="QQQ260612C00480000",
                quantity=2,
                entry_price=3.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 30, tzinfo=UTC),
                status="open_protected",
            )
        )
        await trades.mark_closed(
            "profile-exit",
            exit_order_id="DRY_RUN_EXIT",
            exit_price=2.1,
            exit_filled_quantity=2,
            exit_filled_at=datetime(2026, 6, 3, 14, 0, tzinfo=UTC),
            exit_order_status="FILLED",
            exit_order_type="PAPER",
            exit_rule="initial_stop",
        )

        # A native exit whose exit order id matches the resting stop -> "stop".
        await trades.upsert_trade(
            TradeRecord(
                trade_id="stop-exit",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00481000",
                quantity=1,
                entry_price=4.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 31, tzinfo=UTC),
                status="open_protected",
                stop_order_id="STOP1",
            )
        )
        await trades.mark_closed(
            "stop-exit",
            exit_order_id="STOP1",
            exit_price=2.6,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 3, 14, 1, tzinfo=UTC),
            exit_order_status="FILLED",
        )

        # A native exit whose exit order id matches the resting target -> "target".
        await trades.upsert_trade(
            TradeRecord(
                trade_id="target-exit",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00482000",
                quantity=1,
                entry_price=1.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 32, tzinfo=UTC),
                status="target_active",
                target_order_id="TARGET1",
            )
        )
        await trades.mark_closed(
            "target-exit",
            exit_order_id="TARGET1",
            exit_price=1.8,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 3, 14, 2, tzinfo=UTC),
            exit_order_status="FILLED",
        )

        # exit_mode="hard_flat" (the EOD sweep) -> "hard_flat".
        await trades.upsert_trade(
            TradeRecord(
                trade_id="hard-flat-exit",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00483000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 33, tzinfo=UTC),
                status="exit_pending",
                exit_mode=ExitMode.HARD_FLAT,
            )
        )
        await trades.mark_closed(
            "hard-flat-exit",
            exit_order_id="HARDFLAT1",
            exit_price=1.9,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 3, 14, 3, tzinfo=UTC),
            exit_order_status="FILLED",
        )

        # A native strategy exit with no stop/target match and no exit_mode
        # recorded (the common dry_run/paper shape) -> "strategy" by elimination.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="strategy-exit",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00484000",
                quantity=1,
                entry_price=2.5,
                entry_timestamp=datetime(2026, 6, 3, 13, 34, tzinfo=UTC),
                status="exit_pending",
            )
        )
        await trades.mark_closed(
            "strategy-exit",
            exit_order_id="DRY_RUN_EXIT",
            exit_price=2.2,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 6, 3, 14, 4, tzinfo=UTC),
            exit_order_status="FILLED",
        )

    asyncio.run(seed())

    report = build_daily_report(db_path, trading_date="2026-06-03")
    by_id = {trade["trade_id"]: trade for trade in report["trades"]}

    assert by_id["profile-exit"]["exit_attribution"] == "profile:initial_stop"
    assert by_id["stop-exit"]["exit_attribution"] == "stop"
    assert by_id["target-exit"]["exit_attribution"] == "target"
    assert by_id["hard-flat-exit"]["exit_attribution"] == "hard_flat"
    assert by_id["strategy-exit"]["exit_attribution"] == "strategy"

    assert report["profile_exit_summary"] == {"count": 1, "rule_counts": {"initial_stop": 1}}

    markdown = write_daily_report(db_path, output_dir=tmp_path / "reports", trading_date="2026-06-03").markdown_path.read_text(
        encoding="utf-8"
    )
    assert "| Lane | Symbol | Option | Qty | Entry | Exit Px | Exit | P&L | Status |" in markdown
    assert "profile:initial_stop" in markdown
    assert "- profile exits: `1` (initial_stop)" in markdown

    telegram = render_daily_report_telegram_summary(report)
    assert "Profile exits: 1 (initial_stop)" in telegram


def test_daily_report_open_trade_has_no_exit_attribution(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="still-open",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00485000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 35, tzinfo=UTC),
                status="open_protected",
                stop_order_id="STOP2",
            )
        )

    asyncio.run(seed())

    report = build_daily_report(db_path, trading_date="2026-06-03")
    open_trade = next(t for t in report["trades"] if t["trade_id"] == "still-open")
    assert open_trade["exit_attribution"] is None
    assert report["profile_exit_summary"] == {"count": 0, "rule_counts": {}}


# --------------------------------------------------------------------------- #
# workplan #17 / operator-audit P5: relaxed-evidence shadow lanes trading today.
# --------------------------------------------------------------------------- #


def test_daily_report_surfaces_relaxed_evidence_lanes_trading_today(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    trades = SQLiteTradeStateRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="relaxed-lane-trade",
                deployment_id="mu_shadow_relaxed",
                symbol="ACME",
                option_symbol="ACME260612C01200000",
                quantity=1,
                entry_price=25.98,
                entry_timestamp=datetime(2026, 6, 3, 13, 39, tzinfo=UTC),
                status="open_unprotected",
            )
        )
        # A second deployment that traded today but was NOT compiled with any
        # relaxed gate -> must not appear in the section.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="clean-lane-trade",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260612C00486000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 6, 3, 13, 40, tzinfo=UTC),
                status="open_protected",
            )
        )

    asyncio.run(seed())

    deployments = [
        _minimal_deployment(
            "mu_shadow_relaxed",
            "ACME",
            metadata={"evidence_gates_relaxed": ["mala_evidence_ready:candidate", "activation_candidate:candidate"]},
        ),
        _minimal_deployment("qqq_live", "QQQ"),
        # Deployment carries relaxed gates but did NOT trade today -> excluded.
        _minimal_deployment(
            "spy_shadow_relaxed_idle",
            "SPY",
            metadata={"evidence_gates_relaxed": ["option_trade_ready:candidate"]},
        ),
    ]

    report = build_daily_report(db_path, trading_date="2026-06-03", deployments=deployments)

    assert report["relaxed_evidence_lanes"] == [
        {
            "deployment_id": "mu_shadow_relaxed",
            "evidence_gates_relaxed": ["mala_evidence_ready:candidate", "activation_candidate:candidate"],
        }
    ]

    markdown = write_daily_report(
        db_path, output_dir=tmp_path / "reports", trading_date="2026-06-03", deployments=deployments
    ).markdown_path.read_text(encoding="utf-8")
    assert "## Shadow Lanes on Relaxed Evidence" in markdown
    assert "mu_shadow_relaxed" in markdown
    assert "spy_shadow_relaxed_idle" not in markdown

    telegram = render_daily_report_telegram_summary(report)
    assert "- Relaxed shadow evidence: mu_shadow_relaxed (2 gates)" in telegram
    assert "mala_evidence_ready:candidate" not in telegram


def test_daily_report_without_deployments_omits_relaxed_evidence_section(tmp_path) -> None:
    result = write_daily_report(tmp_path / "missing.db", output_dir=tmp_path / "reports", trading_date="2026-06-03")
    assert result.report["relaxed_evidence_lanes"] == []
    assert "Shadow Lanes on Relaxed Evidence" not in result.markdown_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Item C (2026-07-08 hygiene batch): per-LIVE-lane entry_selector_empty count.
# 2026-07-07 case: SMH's signal was live but the selector filtered out every
# contract -- a filtered-out live signal must not masquerade as a quiet day.
# --------------------------------------------------------------------------- #


def test_daily_report_entry_selector_empty_by_deployment_flags_live_lanes(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)

    async def seed() -> None:
        # LIVE lane (SMH) -- the selector filtered out every contract twice.
        for _ in range(2):
            await events.append(
                "runtime_issue",
                {
                    "category": "entry_selector_empty",
                    "deployment_id": "smh_live",
                    "symbol": "SMH",
                    "action": "entry",
                    "error": "SelectorEmptyError: no contract passed the selector",
                    "stage": "execution_runner",
                },
            )
        # Shadow lane -- same category, must not be conflated with the live count.
        await events.append(
            "runtime_issue",
            {
                "category": "entry_selector_empty",
                "deployment_id": "acme_shadow",
                "symbol": "ACME",
                "action": "entry",
                "error": "SelectorEmptyError: no contract passed the selector",
                "stage": "execution_runner",
            },
        )
        # A different runtime_issue category must not be counted here.
        await events.append(
            "runtime_issue",
            {"category": "dead_lane", "deployment_id": "smh_live", "symbol": "SMH", "error": "no fills"},
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-07-07T14:00:00+00:00'")
        conn.commit()

    deployments = [
        _minimal_deployment("smh_live", "SMH"),
        DeploymentManifest(
            deployment_id="acme_shadow",
            enabled=True,
            symbol="ACME",
            strategy=StrategySpec(key="market_impulse", params={"direction": "short"}),
            execution=ExecutionSpec(profile="default", shadow_only=True),
            risk=RiskSpec(profile="default"),
            source=SourceSpec(metadata={}),
        ),
    ]

    report = build_daily_report(db_path, trading_date="2026-07-07", deployments=deployments)

    assert report["provider_health"]["entry_selector_empty_by_deployment"] == [
        {"deployment_id": "smh_live", "count": 2, "live": True},
        {"deployment_id": "acme_shadow", "count": 1, "live": False},
    ]
    # dead_lane must still be counted separately in the existing summary, and
    # entry_selector_empty must not double up with it.
    assert report["provider_health"]["runtime_issue_counts"] == {"dead_lane": 1, "entry_selector_empty": 3}

    markdown = write_daily_report(
        db_path, output_dir=tmp_path / "reports", trading_date="2026-07-07", deployments=deployments
    ).markdown_path.read_text(encoding="utf-8")
    assert "## Entry Selector Empty (per lane)" in markdown
    assert "`smh_live` [LIVE]: `2`" in markdown
    assert "`acme_shadow` [shadow]: `1`" in markdown

    telegram = render_daily_report_telegram_summary(report)
    assert "LIVE entry_selector_empty: smh_live x2" in telegram
    assert "acme_shadow" not in telegram  # shadow lane must not appear in the live-flagged watch line


def test_daily_report_omits_entry_selector_empty_section_when_none_fired(tmp_path) -> None:
    result = write_daily_report(tmp_path / "missing.db", output_dir=tmp_path / "reports", trading_date="2026-06-03")
    assert result.report["provider_health"]["entry_selector_empty_by_deployment"] == []
    assert "Entry Selector Empty" not in result.markdown_path.read_text(encoding="utf-8")
    assert "entry_selector_empty" not in render_daily_report_telegram_summary(result.report)


# --- Risk rails section tests (operator-audit P3) ---------------------------
def test_daily_report_renders_risk_rails_section_from_startup_event_and_budget(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    cash_budget = SQLiteCashBudgetRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await events.append(
            "risk_manager_startup",
            {
                "rail_a_enabled": True,
                "rail_b_enabled": True,
                "max_daily_drawdown_pct": 2.0,
                "flatten_daily_drawdown_pct": 3.0,
                "demote_window": 10,
                "demote_min_n": 10,
                "demote_threshold_usd": 0.0,
                "validation_warnings": [],
            },
        )
        await cash_budget.upsert_day(
            CashBudgetDay(
                trade_date="2026-06-03",
                account_type="CASH",
                broker_cash_only_buying_power=10000.0,
                usable_budget=8000.0,
                buffer_pct=0.2,
            )
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-06-03T13:30:00+00:00'")
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-06-03")

    risk_rails = report["risk_rails"]
    assert risk_rails["rail_a_enabled"] is True
    assert risk_rails["rail_b_enabled"] is True
    assert risk_rails["usable_budget_usd"] == 8000.0
    assert risk_rails["max_daily_drawdown_pct"] == 2.0
    assert risk_rails["max_daily_drawdown_usd"] == 160.0  # 2% of 8000
    assert risk_rails["flatten_daily_drawdown_pct"] == 3.0
    assert risk_rails["flatten_daily_drawdown_usd"] == 240.0  # 3% of 8000
    assert risk_rails["demote_window"] == 10
    assert risk_rails["demote_min_n"] == 10
    assert risk_rails["demote_threshold_usd"] == 0.0
    assert risk_rails["validation_warnings"] == []

    markdown = render_daily_report_markdown(report)
    assert "## Risk Rails" in markdown
    assert "rail-A(halt) on, rail-B(demote) on" in markdown
    assert "usable budget: `$8,000.00`" in markdown
    assert "tier-1 halt (new entries): `2.00% ($160.00)`" in markdown
    assert "tier-2 flatten (book): `3.00% ($240.00)`" in markdown
    assert "auto-demote: `window 10, min_n 10, threshold $0.00`" in markdown


def test_daily_report_risk_rails_section_surfaces_validation_warnings(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await events.append(
            "risk_manager_startup",
            {
                "rail_a_enabled": True,
                "rail_b_enabled": False,
                "max_daily_drawdown_pct": 2.0,
                "flatten_daily_drawdown_pct": 2.0,
                "demote_window": 10,
                "demote_min_n": 10,
                "demote_threshold_usd": 0.0,
                "validation_warnings": [
                    "flatten_daily_drawdown_pct=-1.0 must be > 0; using default 3.0",
                ],
            },
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-06-03T13:30:00+00:00'")
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-06-03")

    risk_rails = report["risk_rails"]
    assert risk_rails["rail_b_enabled"] is False
    # No cash_budget_days row seeded for this day -> $ figures are unknown.
    assert risk_rails["usable_budget_usd"] is None
    assert risk_rails["max_daily_drawdown_usd"] is None
    assert len(risk_rails["validation_warnings"]) == 1

    markdown = render_daily_report_markdown(report)
    assert "usable budget: `unknown`" in markdown
    assert "tier-1 halt (new entries): `2.00% (n/a)`" in markdown
    assert "validation warnings: `1`" in markdown
    assert "flatten_daily_drawdown_pct=-1.0" in markdown


def test_daily_report_risk_rails_absent_when_no_startup_event(tmp_path) -> None:
    db_path = tmp_path / "missing.db"

    report = build_daily_report(db_path, trading_date="2026-06-03")

    assert report["risk_rails"] is None
    markdown = render_daily_report_markdown(report)
    assert "## Risk Rails" not in markdown


# --- Open-book MTM warning section tests (operator audit P4) ----------------
def test_daily_report_renders_open_drawdown_warn_pct_defaulted_to_tier1(tmp_path) -> None:
    """open_drawdown_warn_pct absent from the startup payload (unset knob,
    matches RiskSettings.open_drawdown_warn_pct=None) -- the report displays
    the SAME "falls back to max_daily_drawdown_pct" value RiskManager.
    effective_open_drawdown_warn_pct would use, not a blank/unknown."""
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    cash_budget = SQLiteCashBudgetRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await events.append(
            "risk_manager_startup",
            {
                "rail_a_enabled": True,
                "rail_b_enabled": True,
                "max_daily_drawdown_pct": 2.0,
                "flatten_daily_drawdown_pct": 3.0,
                "demote_window": 10,
                "demote_min_n": 10,
                "demote_threshold_usd": 0.0,
                "open_drawdown_warn_pct": None,
                "validation_warnings": [],
            },
        )
        await cash_budget.upsert_day(
            CashBudgetDay(
                trade_date="2026-06-03",
                account_type="CASH",
                broker_cash_only_buying_power=10000.0,
                usable_budget=8000.0,
                buffer_pct=0.2,
            )
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-06-03T13:30:00+00:00'")
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-06-03")

    risk_rails = report["risk_rails"]
    assert risk_rails["open_drawdown_warn_pct"] == 2.0  # defaulted to max_daily_drawdown_pct
    assert risk_rails["open_drawdown_warn_usd"] == 160.0  # 2% of 8000
    assert risk_rails["open_drawdown_warnings"] == []

    markdown = render_daily_report_markdown(report)
    assert "open-book MTM warn (awareness only): `2.00% ($160.00)`" in markdown
    assert "open-book MTM warnings fired" not in markdown


def test_daily_report_renders_open_drawdown_warning_events(tmp_path) -> None:
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    events = SQLiteEventRepository(str(db_path), backend=backend)
    cash_budget = SQLiteCashBudgetRepository(str(db_path), backend=backend)

    async def seed() -> None:
        await events.append(
            "risk_manager_startup",
            {
                "rail_a_enabled": True,
                "rail_b_enabled": True,
                "max_daily_drawdown_pct": 2.0,
                "flatten_daily_drawdown_pct": 3.0,
                "demote_window": 10,
                "demote_min_n": 10,
                "demote_threshold_usd": 0.0,
                "open_drawdown_warn_pct": 1.0,
                "validation_warnings": [],
            },
        )
        await cash_budget.upsert_day(
            CashBudgetDay(
                trade_date="2026-06-03",
                account_type="CASH",
                broker_cash_only_buying_power=10000.0,
                usable_budget=10000.0,
                buffer_pct=0.0,
            )
        )
        await events.append(
            "risk_open_drawdown_warning",
            {
                "realized_usd": 0.0,
                "unrealized_open_usd": -350.0,
                "day_mtm_usd": -350.0,
                "warn_threshold_usd": -100.0,
                "warn_pct": 1.0,
                "usable_budget": 10000.0,
                "open_position_count": 2,
                "trade_date": "2026-06-03",
            },
        )

    asyncio.run(seed())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE events SET created_at = '2026-06-03T13:30:00+00:00'")
        conn.commit()

    report = build_daily_report(db_path, trading_date="2026-06-03")

    risk_rails = report["risk_rails"]
    assert risk_rails["open_drawdown_warn_pct"] == 1.0
    assert risk_rails["open_drawdown_warn_usd"] == 100.0
    assert len(risk_rails["open_drawdown_warnings"]) == 1
    warning = risk_rails["open_drawdown_warnings"][0]
    assert warning["day_mtm_usd"] == -350.0
    assert warning["open_position_count"] == 2

    markdown = render_daily_report_markdown(report)
    assert "open-book MTM warn (awareness only): `1.00% ($100.00)`" in markdown
    assert "open-book MTM warnings fired: `1`" in markdown
    assert "day MTM -$350.00 (realized $0.00 + unrealized open -$350.00 across 2 position(s)) <= threshold -$100.00" in markdown
# --- end open-book MTM warning section tests ---------------------------------
# --- end risk rails section tests -------------------------------------------
