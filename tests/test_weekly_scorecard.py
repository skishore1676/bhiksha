import asyncio
from datetime import UTC, datetime

from bhiksha.config.models import (
    DeploymentManifest,
    ExecutionSpec,
    RiskSpec,
    SourceSpec,
    StrategySpec,
)
from bhiksha.domain.models import PartialFillRecord, TradeRecord
from bhiksha.ops.weekly_scorecard import (
    build_weekly_scorecard,
    render_weekly_scorecard_markdown,
    render_weekly_scorecard_telegram_summary,
    write_weekly_scorecard,
)
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteTradeStateRepository


def _deployment(deployment_id: str, symbol: str, *, shadow: bool, relaxed: list[str] | None = None) -> DeploymentManifest:
    return DeploymentManifest(
        deployment_id=deployment_id,
        enabled=True,
        symbol=symbol,
        strategy=StrategySpec(key="market_impulse", params={"direction": "short"}),
        execution=ExecutionSpec(profile="default", shadow_only=shadow),
        risk=RiskSpec(profile="default"),
        source=SourceSpec(metadata={"evidence_gates_relaxed": relaxed} if relaxed else {}),
    )


def _repo(tmp_path):
    db_path = tmp_path / "bhiksha.db"
    backend = SQLiteBackend(str(db_path))
    return db_path, SQLiteTradeStateRepository(str(db_path), backend=backend)


# --------------------------------------------------------------------------- #
# P&L must include banked partial legs (the whole point -- trade_sessions holds
# only the runner residual after a T1 bank).
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_pnl_includes_banked_partial_legs(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        # A laddered LIVE winner mirroring 2026-07-09 QQQ: 10-lot entry @ 1.94,
        # bank 6 @ 2.72 (T1 partial), runner 4 @ 3.43 (T2). trade_sessions holds
        # only the residual (4) + runner exit; the +$468 partial lives only in
        # trade_partial_fills.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="qqq-ladder",
                deployment_id="qqq_short_live",
                symbol="QQQ",
                option_symbol="QQQ260713C00560000",
                quantity=4,  # residual after the T1 bank
                entry_price=1.94,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
                status="target_active",
                entry_order_id="LIVE_ENTRY",
                can_ladder=True,
            )
        )
        await trades.record_partial_fill(
            PartialFillRecord(
                id=None,
                trade_id="qqq-ladder",
                deployment_id="qqq_short_live",
                symbol="QQQ",
                option_symbol="QQQ260713C00560000",
                closed_quantity=6,
                order_id="PARTIAL1",
                exit_rule="target_1_partial",
                submitted_at=datetime(2026, 7, 9, 14, 18, tzinfo=UTC),
                fill_price=2.72,
                fill_quantity=6,
                filled_at=datetime(2026, 7, 9, 14, 18, tzinfo=UTC),
                order_status="FILLED",
                order_type="MARKET",
                origin="partial_scale",
            )
        )
        await trades.mark_closed(
            "qqq-ladder",
            exit_order_id="RUNNER_EXIT",
            exit_price=3.43,
            exit_filled_quantity=4,
            exit_filled_at=datetime(2026, 7, 9, 14, 30, tzinfo=UTC),
            exit_order_status="FILLED",
            exit_order_type="LIMIT",
            exit_rule="target_2_runner",
        )

    asyncio.run(seed())

    report = build_weekly_scorecard(db_path, week_start="2026-07-07", week_end="2026-07-09")

    # Partial (2.72-1.94)*6*100 = 468 + runner (3.43-1.94)*4*100 = 596 -> 1064.
    trade = next(lane for lane in report["lanes"] if lane["deployment_id"] == "qqq_short_live")
    assert trade["total_pnl_usd"] == 1064.0
    assert report["headline"]["live"]["total_pnl_usd"] == 1064.0
    # Blended return is on the ORIGINAL 10-lot cost basis (1.94*10*100 = 1940).
    assert trade["avg_return_pct"] == 54.85
    # The final exit is attributed to the runner rule (not the partial rule).
    assert trade["exit_rule_counts"] == {"profile:target_2_runner": 1}


def test_weekly_scorecard_skips_abandoned_and_unconfirmed_partial_legs(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="amd-partial",
                deployment_id="amd_short_live",
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=2,
                entry_price=10.0,
                entry_timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
                status="target_active",
                entry_order_id="LIVE_ENTRY",
                can_ladder=True,
            )
        )
        # A confirmed banked leg -> counts.
        await trades.record_partial_fill(
            PartialFillRecord(
                id=None,
                trade_id="amd-partial",
                deployment_id="amd_short_live",
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                closed_quantity=1,
                order_id="P_OK",
                exit_rule="target_1_partial",
                submitted_at=datetime(2026, 7, 8, 14, 5, tzinfo=UTC),
                fill_price=13.0,
                fill_quantity=1,
                filled_at=datetime(2026, 7, 8, 14, 5, tzinfo=UTC),
                order_status="FILLED",
                order_type="MARKET",
            )
        )
        # An abandoned leg with no fill -> must be skipped, not invented.
        await trades.record_partial_fill(
            PartialFillRecord(
                id=None,
                trade_id="amd-partial",
                deployment_id="amd_short_live",
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                closed_quantity=1,
                order_id="P_ABANDONED",
                exit_rule="target_1_partial",
                submitted_at=datetime(2026, 7, 8, 14, 6, tzinfo=UTC),
                fill_price=None,
                order_status="WORKING",
                order_type="MARKET",
                abandoned_reason="sweep_gave_up",
            )
        )
        await trades.mark_closed(
            "amd-partial",
            exit_order_id="RUNNER_EXIT",
            exit_price=14.0,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 8, 14, 30, tzinfo=UTC),
            exit_order_status="FILLED",
            exit_order_type="LIMIT",
            exit_rule="target_1_partial",
        )

    asyncio.run(seed())

    report = build_weekly_scorecard(db_path, week_start="2026-07-06", week_end="2026-07-10")
    lane = next(lane for lane in report["lanes"] if lane["deployment_id"] == "amd_short_live")
    # Confirmed partial (13-10)*1*100 = 300 + runner (14-10)*1*100 = 400 -> 700.
    # The abandoned unconfirmed leg contributes nothing.
    assert lane["total_pnl_usd"] == 700.0


# --------------------------------------------------------------------------- #
# Live/shadow split: prefer the deployment manifest, fall back to trade rows.
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_mode_split_uses_manifest_then_trade_rows(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-1",
                deployment_id="qqq_short_live",
                symbol="QQQ",
                option_symbol="QQQ260713C00560000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
                status="open_protected",
                entry_order_id="LIVE_ENTRY",
            )
        )
        await trades.mark_closed(
            "live-1", exit_order_id="X1", exit_price=3.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 8, 15, 0, tzinfo=UTC), exit_order_status="FILLED", exit_order_type="LIMIT",
        )
        await trades.upsert_trade(
            TradeRecord(
                trade_id="shadow-1",
                deployment_id="meta_short_shadow",
                symbol="META",
                option_symbol="META260713C00580000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.mark_closed(
            "shadow-1", exit_order_id="DRY_RUN", exit_price=1.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 8, 15, 0, tzinfo=UTC), exit_order_status="FILLED", exit_order_type="PAPER",
        )

    asyncio.run(seed())

    deployments = [
        _deployment("qqq_short_live", "QQQ", shadow=False),
        _deployment("meta_short_shadow", "META", shadow=True, relaxed=["mala_evidence_ready: watch_only"]),
    ]
    report = build_weekly_scorecard(
        db_path, week_start="2026-07-06", week_end="2026-07-10", deployments=deployments
    )

    assert report["headline"]["live"]["total_pnl_usd"] == 100.0
    assert report["headline"]["shadow"]["total_pnl_usd"] == -100.0
    live_lane = next(lane for lane in report["lanes"] if lane["deployment_id"] == "qqq_short_live")
    shadow_lane = next(lane for lane in report["lanes"] if lane["deployment_id"] == "meta_short_shadow")
    assert live_lane["mode"] == "live"
    assert shadow_lane["mode"] == "shadow"
    assert shadow_lane["evidence_gates_relaxed"] == ["mala_evidence_ready: watch_only"]

    # Without deployments, the mode falls back to the SHADOW_ENTRY / PAPER trade
    # markers and the evidence-gate flag is unknown.
    report_no_dep = build_weekly_scorecard(db_path, week_start="2026-07-06", week_end="2026-07-10")
    shadow_lane_nd = next(lane for lane in report_no_dep["lanes"] if lane["deployment_id"] == "meta_short_shadow")
    assert shadow_lane_nd["mode"] == "shadow"
    assert shadow_lane_nd["evidence_gates_relaxed"] is None


def test_weekly_scorecard_does_not_relabel_historical_shadow_after_promotion(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        await trades.upsert_trade(TradeRecord(
            trade_id="historical-shadow", deployment_id="promoted_strategy", symbol="META",
            option_symbol="META260713P00580000", quantity=1, entry_price=2.0,
            entry_timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
            status="open_protected", entry_order_id="SHADOW_ENTRY",
        ))
        await trades.mark_closed(
            "historical-shadow", exit_order_id="DRY_RUN", exit_price=3.0,
            exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 8, 15, 0, tzinfo=UTC),
            exit_order_status="FILLED", exit_order_type="PAPER",
        )

    asyncio.run(seed())
    # Today's manifest says live, but immutable trade-time markers say shadow.
    report = build_weekly_scorecard(
        db_path, week_start="2026-07-06", week_end="2026-07-10",
        deployments=[_deployment("promoted_strategy", "META", shadow=False)],
    )

    assert report["headline"]["shadow"]["total_pnl_usd"] == 100.0
    assert report["headline"]["live"]["trades"] == 0


# --------------------------------------------------------------------------- #
# Profile vs legacy bucketing (a profile:<rule> final exit vs everything else).
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_profile_vs_legacy_buckets(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        # Live profile exit (winner).
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-profile", deployment_id="qqq_short_live", symbol="QQQ",
                option_symbol="QQQ260713C00560000", quantity=1, entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC), status="target_active",
                entry_order_id="LIVE_ENTRY",
            )
        )
        await trades.mark_closed(
            "live-profile", exit_order_id="X", exit_price=3.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="LIMIT", exit_rule="target_2_runner",
        )
        # Live legacy stop (loser) -- exit order id matches the resting stop.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-legacy", deployment_id="nvda_short_live", symbol="NVDA",
                option_symbol="NVDA260713P00160000", quantity=1, entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="LIVE_ENTRY", stop_order_id="STOP1",
            )
        )
        await trades.mark_closed(
            "live-legacy", exit_order_id="STOP1", exit_price=1.3, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
        )

    asyncio.run(seed())

    report = build_weekly_scorecard(db_path, week_start="2026-07-07", week_end="2026-07-09")
    pvl = report["profile_vs_legacy"]
    assert pvl["live"]["profile"] == {"n": 1, "wins": 1, "total_pnl_usd": 100.0, "avg_return_pct": 50.0}
    assert pvl["live"]["legacy"] == {"n": 1, "wins": 0, "total_pnl_usd": -70.0, "avg_return_pct": -35.0}
    assert pvl["overall"]["profile"]["total_pnl_usd"] == 100.0
    assert pvl["overall"]["legacy"]["total_pnl_usd"] == -70.0
    assert "OVERSTATE stop slippage" in pvl["caveat"]


# --------------------------------------------------------------------------- #
# Promotion candidates: shadow + n>=5 + positive P&L + no data-quality flag.
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_promotion_candidate_filtering(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        # Qualifying shadow lane: 5 closed, net positive.
        for i in range(5):
            tid = f"good-{i}"
            await trades.upsert_trade(
                TradeRecord(
                    trade_id=tid, deployment_id="spy_long_shadow", symbol="SPY",
                    option_symbol="SPY260713C00600000", quantity=1, entry_price=1.0,
                    entry_timestamp=datetime(2026, 7, 9, 14, i, tzinfo=UTC), status="open_protected",
                    entry_order_id="SHADOW_ENTRY",
                )
            )
            await trades.mark_closed(
                tid, exit_order_id="DRY_RUN", exit_price=1.2, exit_filled_quantity=1,
                exit_filled_at=datetime(2026, 7, 9, 15, i, tzinfo=UTC), exit_order_status="FILLED",
                exit_order_type="PAPER",
            )
        # Sample-qualifying but net-negative shadow lane -> near-miss, not candidate.
        for i in range(5):
            tid = f"bad-{i}"
            await trades.upsert_trade(
                TradeRecord(
                    trade_id=tid, deployment_id="meta_short_shadow", symbol="META",
                    option_symbol="META260713C00580000", quantity=1, entry_price=2.0,
                    entry_timestamp=datetime(2026, 7, 9, 14, i, tzinfo=UTC), status="open_protected",
                    entry_order_id="SHADOW_ENTRY",
                )
            )
            await trades.mark_closed(
                tid, exit_order_id="DRY_RUN", exit_price=1.5, exit_filled_quantity=1,
                exit_filled_at=datetime(2026, 7, 9, 15, i, tzinfo=UTC), exit_order_status="FILLED",
                exit_order_type="PAPER",
            )
        # Positive but too-small shadow lane -> excluded (n < 5).
        await trades.upsert_trade(
            TradeRecord(
                trade_id="small-1", deployment_id="pltr_short_shadow", symbol="PLTR",
                option_symbol="PLTR260713C00030000", quantity=1, entry_price=1.0,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.mark_closed(
            "small-1", exit_order_id="DRY_RUN", exit_price=2.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="PAPER",
        )
        # A LIVE lane that is positive with n>=5 must NEVER be a promotion
        # candidate (promotion is a shadow-only concept).
        for i in range(5):
            tid = f"live-{i}"
            await trades.upsert_trade(
                TradeRecord(
                    trade_id=tid, deployment_id="qqq_short_live", symbol="QQQ",
                    option_symbol="QQQ260713C00560000", quantity=1, entry_price=1.0,
                    entry_timestamp=datetime(2026, 7, 9, 14, i, tzinfo=UTC), status="open_protected",
                    entry_order_id="LIVE_ENTRY",
                )
            )
            await trades.mark_closed(
                tid, exit_order_id="X", exit_price=1.3, exit_filled_quantity=1,
                exit_filled_at=datetime(2026, 7, 9, 15, i, tzinfo=UTC), exit_order_status="FILLED",
                exit_order_type="LIMIT",
            )

    asyncio.run(seed())

    deployments = [
        _deployment("spy_long_shadow", "SPY", shadow=True, relaxed=["mala_evidence_ready: watch_only"]),
        _deployment("meta_short_shadow", "META", shadow=True),
        _deployment("pltr_short_shadow", "PLTR", shadow=True),
        _deployment("qqq_short_live", "QQQ", shadow=False),
    ]
    report = build_weekly_scorecard(
        db_path, week_start="2026-07-07", week_end="2026-07-09", deployments=deployments
    )
    promotion = report["promotion_candidates"]

    candidate_ids = [c["deployment_id"] for c in promotion["candidates"]]
    assert candidate_ids == ["spy_long_shadow"]
    candidate = promotion["candidates"][0]
    assert candidate["closed"] == 5
    assert candidate["total_pnl_usd"] == 100.0  # 5 * (1.2-1.0)*100
    assert candidate["evidence_gates_relaxed"] == ["mala_evidence_ready: watch_only"]
    assert "M7 validation" in candidate["note"]

    near_ids = [m["deployment_id"] for m in promotion["near_misses"]]
    assert near_ids == ["meta_short_shadow"]
    assert promotion["near_misses"][0]["disqualified_by"] == "non_positive_total_pnl"


# --------------------------------------------------------------------------- #
# Live cumulative-by-day runs from the experiment start, not the week window.
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_live_cumulative_by_day_from_experiment_start(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        # 07-02 live winner (before the week window) -- must still appear in the
        # cumulative line.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="d1", deployment_id="nvda_short_live", symbol="NVDA",
                option_symbol="NVDA260703C00160000", quantity=1, entry_price=1.0,
                entry_timestamp=datetime(2026, 7, 2, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="LIVE_ENTRY",
            )
        )
        await trades.mark_closed(
            "d1", exit_order_id="X", exit_price=2.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 2, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="LIMIT",
        )
        # 07-09 live loser inside the week.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="d2", deployment_id="qqq_short_live", symbol="QQQ",
                option_symbol="QQQ260713C00560000", quantity=1, entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="LIVE_ENTRY",
            )
        )
        await trades.mark_closed(
            "d2", exit_order_id="X", exit_price=1.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="LIMIT",
        )
        # A shadow trade must NOT enter the live cumulative.
        await trades.upsert_trade(
            TradeRecord(
                trade_id="s1", deployment_id="meta_short_shadow", symbol="META",
                option_symbol="META260713C00580000", quantity=1, entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 3, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
        await trades.mark_closed(
            "s1", exit_order_id="DRY_RUN", exit_price=5.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 3, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="PAPER",
        )

    asyncio.run(seed())

    report = build_weekly_scorecard(
        db_path, week_start="2026-07-07", week_end="2026-07-09", experiment_start="2026-07-02"
    )
    cumulative = report["live_cumulative"]
    assert cumulative["since"] == "2026-07-02"
    assert cumulative["by_day"] == [
        {"day": "2026-07-02", "trades": 1, "pnl_usd": 100.0},
        {"day": "2026-07-09", "trades": 1, "pnl_usd": -100.0},
    ]
    assert cumulative["total_pnl_usd"] == 0.0
    assert cumulative["total_trades"] == 2


# --------------------------------------------------------------------------- #
# Renderer: the report has the expected sections and writes both artifacts.
# --------------------------------------------------------------------------- #


def test_weekly_scorecard_renderer_sections(tmp_path) -> None:
    db_path, trades = _repo(tmp_path)

    async def seed() -> None:
        await trades.upsert_trade(
            TradeRecord(
                trade_id="live-1", deployment_id="qqq_short_live", symbol="QQQ",
                option_symbol="QQQ260713C00560000", quantity=1, entry_price=2.0,
                entry_timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC), status="open_protected",
                entry_order_id="LIVE_ENTRY",
            )
        )
        await trades.mark_closed(
            "live-1", exit_order_id="X", exit_price=3.0, exit_filled_quantity=1,
            exit_filled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), exit_order_status="FILLED",
            exit_order_type="LIMIT", exit_rule="target_2_runner",
        )

    asyncio.run(seed())

    result = write_weekly_scorecard(
        db_path, output_dir=tmp_path / "reports", week_start="2026-07-07", week_end="2026-07-09"
    )
    assert result.json_path.exists()
    assert result.markdown_path.exists()

    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "# Bhiksha Weekly Scorecard - 2026-07-07 to 2026-07-09" in markdown
    assert "## Week Headline" in markdown
    assert "## Per-Lane" in markdown
    assert "## Profile Exits vs Legacy Exits" in markdown
    assert "## Promotion Candidates" in markdown
    assert "## Live Experiment Cumulative (since 2026-07-02)" in markdown
    assert "profile:target_2_runner" in markdown

    telegram = render_weekly_scorecard_telegram_summary(result.report, markdown_path=result.markdown_path)
    assert "Bhiksha Weekly Scorecard - 2026-07-07 to 2026-07-09" in telegram
    assert "Profile vs legacy (all)" in telegram
    assert "Promotion candidates: 0" in telegram


def test_weekly_scorecard_missing_db_is_empty_but_renders(tmp_path) -> None:
    result = write_weekly_scorecard(
        tmp_path / "missing.db", output_dir=tmp_path / "reports",
        week_start="2026-07-07", week_end="2026-07-09",
    )
    assert result.report["headline"]["total"]["total_pnl_usd"] == 0.0
    assert result.report["lanes"] == []
    assert result.report["promotion_candidates"]["candidates"] == []
    markdown = render_weekly_scorecard_markdown(result.report)
    assert "# Bhiksha Weekly Scorecard - 2026-07-07 to 2026-07-09" in markdown
    assert "None this week" in markdown
