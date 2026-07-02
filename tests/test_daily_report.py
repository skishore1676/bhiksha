import asyncio
from datetime import UTC, datetime
import json
import sqlite3

from bhiksha.domain.models import TradeRecord
from bhiksha.ops.daily_report import build_daily_report, render_daily_report_telegram_summary, write_daily_report
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository


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
    assert report["data_quality_warnings"][0]["symbol"] == "ACME"
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
    assert "Open: live 2, shadow 1, protected 1, target active 1, unprotected 1, exit pending 0" in body
    assert "live QQQ QQQ260612C00480000 qty 2" in body


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
    assert "Open: live 0, shadow 0, protected 0, target active 0, unprotected 0, exit pending 0" in body
    assert "P&L: live $0.00 (0 trades), shadow $412.00 (2 trades)" in body
    assert "Data quality: 1 warning(s); first=ACME" in body
    assert str(tmp_path / "report.md") in body
    assert len(body.splitlines()) <= 9


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
