"""Regression cases for the source-only entry/exit takeover."""
import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bhiksha.active_plan.compiler import _exit_spec_fields_from_management_policy_spec
from bhiksha.config.exit_catalog import ExitProfileConfig, load_exit_profiles_sheet_rows
from bhiksha.config.models import AppConfig, ExitSpec
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.execution.pricing import select_entry_limit
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository, QuoteTapeMark, analyze_cases
from bhiksha.ops.exit_edge_live import ExitEdgeLiveRecorder, QUOTE_SOURCE, QUOTE_FEED, INDEPENDENT_QUOTE_FEED
from bhiksha.ops.exit_comparisons_sheet import build_exit_comparisons_scorecard
from bhiksha.state.position_tracker import PositionTracker
from historical_config import historical_deployment


def profile(name="baseline", **updates):
    return ExitProfileConfig.model_validate({
        "exit_profile_id": name, "trade_archetype": "TREND_CONTINUATION", "exit_family": "staged_r_ladder",
        "target_1_r": 1, "target_2_r": 2, "target_1_quantity": .5,
        "initial_stop_pct": .30, "disaster_stop_pct": .40, "no_progress_seconds": 60,
        "giveback_policy": "OFF", "breakeven_after_t1": True, "eod_flat": True,
        "hard_flat_time_et": "15:55", **updates,
    })


def deployment():
    baseline = _exit_spec_fields_from_management_policy_spec(profile().to_management_policy_spec_dict())
    candidate = _exit_spec_fields_from_management_policy_spec(profile("patient", no_progress_seconds=180).to_management_policy_spec_dict())
    original = historical_deployment("market_impulse_qqq_short_v1")
    return original.model_copy(update={
        "enabled": True,
        "execution": original.execution.model_copy(update={"shadow_only": True, "entry_window_start_et": None, "entry_window_end_et": None}),
        "exit": ExitSpec.model_validate({**original.exit.model_dump(), **baseline, "management_exit": "baseline",
            "compare_exits": ["baseline", "patient"], "compare_exit_policies": [baseline["exit_policy_snapshot"], candidate["exit_policy_snapshot"]],
            "use_profit_target": False, "profit_target_multiple": None}),
    })


def test_catalog_has_no_fallback_and_rejects_invalid_or_unsupported_rows():
    assert load_exit_profiles_sheet_rows([]) == {}
    valid = profile().model_dump()
    for updates in ({"initial_stop_pct": "oops"}, {"eod_flat": False}, {"exit_family": "structural_stop"},
                    {"mispelled_target": 2}, {"target_1_quantity": float("nan")}, {"target_2_r": .1}):
        with pytest.raises(ValueError):
            load_exit_profiles_sheet_rows([{**valid, **updates}])
    with pytest.raises(ValueError, match="duplicate"):
        load_exit_profiles_sheet_rows([valid, valid])
    with pytest.raises(ValueError):
        load_exit_profiles_sheet_rows([{"exit_profile_id": "empty"}])


def test_range_expansion_sheet_can_freeze_an_overnight_three_session_policy():
    raw = profile("range_expansion_swing", trade_archetype="RANGE_EXPANSION",
                  eod_flat=False, no_progress_seconds=None,
                  max_hold_seconds=int(19.5 * 3600)).model_dump()
    frozen = load_exit_profiles_sheet_rows([raw])["range_expansion_swing"].to_management_policy_spec_dict()
    assert frozen["eod_flat"] is False
    assert frozen["no_progress_seconds"] is None
    assert frozen["max_hold_seconds"] == 70_200


def test_expired_option_censors_unfinished_candidate_without_new_quote(tmp_path):
    dep = deployment()
    entry = datetime(2026, 9, 25, 14, tzinfo=UTC)
    recorder = ExitEdgeLiveRecorder(
        db_path=tmp_path / "tape.db", status_path=tmp_path / "status.json", role="observer",
    )
    _, payload = recorder._registration_payloads(
        deployment=dep, trade_id="expired", option_symbol="QQQ260925P00500000",
        entry_timestamp=entry, entry_premium=2.0, quantity=2,
        entry_context={"entry_fill_kind": "modeled_ask_touch"},
    )
    repo = ProspectiveQuoteTapeRepository(tmp_path / "tape.db")
    repo.initialize()
    repo.register_cohort(payload)
    recorder.refresh_active_from_store()
    recorder.censor_expired_options(datetime(2026, 9, 25, 21, tzinfo=UTC))
    assert recorder.active_option_symbols() == ("QQQ260925P00500000",)
    recorder.censor_expired_options(datetime(2026, 9, 28, 13, 30, tzinfo=UTC))
    assert recorder.active_option_symbols() == ()
    assert repo.load_case(payload["cohort_id"]).persisted_censor_reason == "option_expired_before_candidate_completed"
    assert analyze_cases([repo.load_case(payload["cohort_id"])])["cases"][0]["status"] != "paired"


@pytest.mark.parametrize("dynamic", [False, True])
def test_named_comparisons_persist_continue_after_baseline_and_keep_entry_mode(tmp_path, dynamic):
    dep = deployment()
    if dynamic:
        candidate = profile("patient", exit_family="dynamic_envelope", no_progress_seconds=180,
                            risk_envelope_enabled=True, risk_envelope_activation_r=.5,
                            risk_envelope_initial_floor_r=-1, risk_envelope_floor_at_t1_r=0,
                            risk_envelope_curvature=1.5, risk_envelope_ratchet_step_r=.1)
        dep.exit.compare_exit_policies[1] = _exit_spec_fields_from_management_policy_spec(
            candidate.to_management_policy_spec_dict())["exit_policy_snapshot"]
    entry = datetime(2026, 9, 18, 14, tzinfo=UTC)
    recorder = ExitEdgeLiveRecorder(db_path=tmp_path / "tape.db", status_path=tmp_path / "health.json")
    attempt, payload = recorder._registration_payloads(
        deployment=dep, trade_id="paired", option_symbol="QQQ260918P00500000",
        entry_timestamp=entry, entry_premium=2, quantity=2,
        entry_context={"entry_fill_kind": "modeled_ask_touch"},
    )
    assert attempt["eligible"], attempt
    repo = ProspectiveQuoteTapeRepository(tmp_path / "tape.db")
    repo.initialize()
    repo.register_cohort(payload)
    def append(seq, secs, bid):
        at = entry + timedelta(seconds=secs)
        repo.append_quote(payload["cohort_id"], QuoteTapeMark(seq, QUOTE_SOURCE, QUOTE_FEED, at, at, bid, bid + .05))
    append(1, 61, 2.0)
    append(2, 62, 1.95)
    row = analyze_cases([repo.load_case(payload["cohort_id"])])["cases"][0]
    assert row["status"] == "insufficient_data"
    assert row["named_exit_outcomes"]["baseline"] is not None
    assert row["named_exit_outcomes"]["patient"] is None
    append(3, 181, 2.0)
    append(4, 182, 2.0)
    report = analyze_cases([repo.load_case(payload["cohort_id"])])
    row = report["cases"][0]
    assert row["status"] == "paired"
    assert row["candidate_delta_pnl_usd"]["patient"] == 10.0
    summary = report["summary"]["named_comparisons"][0]
    assert summary["entry_fill_kind"] == "modeled_ask_touch"
    assert summary["paired"] == 1
    assert summary["decision"] == "descriptive_only_no_automatic_promotion"
    assert row["cohort_dimensions"]["authorization_mode"] == "shadow"
    # Later edits do not change the stored management or candidate settings.
    dep.exit.compare_exit_policies[1]["no_progress_seconds"] = 10
    assert repo.load_case(payload["cohort_id"]).experiment["named_profiles"][1]["no_progress_seconds"] == 180


def test_clean_named_pair_survives_later_gap_while_third_arm_continues(tmp_path):
    dep = deployment()
    later = profile("very_patient", no_progress_seconds=500)
    third = _exit_spec_fields_from_management_policy_spec(later.to_management_policy_spec_dict())["exit_policy_snapshot"]
    dep.exit.compare_exit_policies.append(third)
    entry = datetime(2026, 9, 23, 14, tzinfo=UTC)
    registration = ExitEdgeLiveRecorder(db_path=tmp_path / "edge.db", status_path=tmp_path / "register.json", role="registration")
    _, payload = registration._registration_payloads(
        deployment=dep, trade_id="three", option_symbol="QQQ260925P00500000",
        entry_timestamp=entry, entry_premium=2.0, quantity=2,
        entry_context={"entry_fill_kind": "modeled_ask_touch"},
    )
    repo = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    repo.initialize()
    repo.register_cohort(payload)
    seconds = [30, 61, 62, 90, 120, 150, 181, 182, 210, 510, 525]
    for seq, elapsed in enumerate(seconds, start=1):
        at = entry + timedelta(seconds=elapsed)
        repo.append_quote(payload["cohort_id"], QuoteTapeMark(
            seq, QUOTE_SOURCE, INDEPENDENT_QUOTE_FEED, at, at, 2.0, 2.05,
        ))
    case = repo.load_case(payload["cohort_id"])
    assert len(case.observation_gaps) == 1
    assert case.observation_gaps[0].last_sequence == 9
    assert case.observation_gaps[0].first_resumed_sequence == 10
    row = analyze_cases([case])["cases"][0]
    assert row["status"] == "gap_affected"
    assert row["arm_evidence_status"]["baseline"] == "complete_clean"
    assert row["arm_evidence_status"]["patient"] == "complete_clean"
    assert row["arm_evidence_status"]["very_patient"] == "gap_affected_diagnostic"
    assert "patient" in row["clean_candidate_delta_pnl_usd"]
    assert "very_patient" not in row["clean_candidate_delta_pnl_usd"]
    summary = analyze_cases([case])["summary"]["named_comparisons"][0]
    assert summary["candidate_vs_management"]["patient"]["clean_pair_count"] == 1
    assert summary["candidate_vs_management"]["very_patient"]["clean_pair_count"] == 0
    signal_db = tmp_path / "signals.db"
    with sqlite3.connect(signal_db) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE TABLE trade_sessions (trade_id TEXT, entry_order_id TEXT, status TEXT, "
                     "entry_timestamp TEXT, option_symbol TEXT, entry_price REAL, quantity INTEGER)")
    scorecard = build_exit_comparisons_scorecard(
        tmp_path / "edge.db", signal_db_path=signal_db, trading_date=entry.date())
    assert scorecard["exits"]["registered"] == 1
    assert scorecard["exits"]["clean"] == 1
    assert scorecard["exits"]["censored"] == 1
    assert scorecard["exits"]["rows"][0][3] == "Baseline"
    assert scorecard["exits"]["rows"][0][4] == "1 / 1"
    assert scorecard["exits"]["detail"][0][0] == "three"


def test_post_gap_replay_retains_partial_leg_and_original_holding_clock(tmp_path):
    dep = deployment()
    entry = datetime(2026, 9, 23, 14, tzinfo=UTC)
    registration = ExitEdgeLiveRecorder(db_path=tmp_path / "edge.db", status_path=tmp_path / "register.json", role="registration")
    _, payload = registration._registration_payloads(
        deployment=dep, trade_id="partial", option_symbol="QQQ260925P00500000",
        entry_timestamp=entry, entry_premium=2.0, quantity=2,
        entry_context={"entry_fill_kind": "broker_confirmed"},
    )
    repo = ProspectiveQuoteTapeRepository(tmp_path / "edge.db")
    repo.initialize()
    repo.register_cohort(payload)
    for seq, (elapsed, bid) in enumerate([(15, 2.0), (30, 2.7), (45, 2.75),
                                           (345, 1.9), (360, 1.85)], start=1):
        at = entry + timedelta(seconds=elapsed)
        repo.append_quote(payload["cohort_id"], QuoteTapeMark(
            seq, QUOTE_SOURCE, INDEPENDENT_QUOTE_FEED, at, at, bid, bid + .05,
        ))
    case = repo.load_case(payload["cohort_id"])
    row = analyze_cases([case])["cases"][0]
    primary = row["named_exit_outcomes"]["baseline"]
    assert row["status"] == "gap_affected"
    assert primary is not None
    assert len(primary["legs"]) == 2
    assert primary["legs"][0]["fill_at"] == (entry + timedelta(seconds=45)).isoformat()
    assert primary["time_in_trade_seconds"] == 360
    assert row["arm_evidence_status"]["baseline"] == "gap_affected_diagnostic"


def test_paper_limit_needs_later_fresh_ask_and_expires_without_fill():
    async def run():
        dep = deployment()
        now = datetime(2026, 9, 18, 14, tzinfo=UTC)
        decision = SignalDecision(deployment_id=dep.deployment_id, symbol=dep.symbol, timestamp=now,
                                  signal=True, direction=SignalDirection.SHORT, reason=[], features={})
        plan = TradePlan("paper", dep.deployment_id, dep.symbol, SignalDirection.SHORT, "QQQ260918P00500000",
                         2, 2.0, ["approved"], entry_timestamp=now)
        quote = PublicQuote(plan.option_symbol, bid=1.9, ask=2.1, open_interest=1000,
                            quote_timestamp=(now + timedelta(seconds=1)).isoformat(), quote_timestamp_field="quoteTimestamp")
        manager = SimpleNamespace(get_option_quote=AsyncMock(return_value=quote), close=AsyncMock())
        planner = SimpleNamespace(position_tracker=PositionTracker(), order_manager=manager, close=AsyncMock())
        recorder = MagicMock()
        supervisor = ExecutionSupervisor(planner=planner, exit_edge_recorder=recorder)
        supervisor._paper_entries[plan.trade_id] = (dep, decision, plan, now, now + timedelta(seconds=10))
        supervisor.lifecycle_store.begin_entry(dep.symbol, dep.deployment_id, order_id="PAPER_PENDING")
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=2))
        assert planner.position_tracker.total_open_positions == 0
        assert not recorder.prepare_registration.called
        quote.ask = 2.0
        quote.quote_timestamp = now.isoformat()  # reused entry quote cannot fill
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=3))
        assert planner.position_tracker.total_open_positions == 0
        quote.quote_timestamp = (now + timedelta(seconds=4)).isoformat()
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=5))
        assert planner.position_tracker.total_open_positions == 1
        assert recorder.prepare_registration.call_args.kwargs["entry_context"]["entry_fill_kind"] == "modeled_ask_touch"
        assert not supervisor._paper_entries
        second = replace(plan, trade_id="expired")
        supervisor._paper_entries[second.trade_id] = (dep, decision, second, now, now + timedelta(seconds=1))
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=11))
        assert not supervisor._paper_entries
        assert recorder.prepare_registration.call_count == 1
        assert recorder.try_queue_registration.call_count == 1
    asyncio.run(run())


def test_price_seeking_keeps_hard_guards_and_rounds_down():
    quote = PublicQuote("OPTION", bid=1.01, ask=1.10, open_interest=100,
                        quote_timestamp=datetime.now(UTC).isoformat(), quote_timestamp_field="quoteTimestamp")
    result = select_entry_limit(quote, {"entry_pricing_mode": "price_seeking", "min_open_interest": 10})
    assert result.approved and result.limit_price < quote.mid
    pressured = select_entry_limit(quote, {"entry_pricing_mode": "price_seeking", "min_open_interest": 200})
    assert pressured.approved and "open_interest_below_preferred" in pressured.evidence()["liquidity_warnings"]
    assert not select_entry_limit(replace(quote, ask=float("nan")), {"entry_pricing_mode": "price_seeking"}).approved


def test_listed_expiry_walk_skips_ineligible_expiry_and_obeys_ceiling():
    from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest
    from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector
    req = OptionSelectionRequest("lane", "XYZ", SignalDirection.LONG, datetime.now(UTC),
        "single_leg_long_premium_v1", {"long_signal_contract_type": "CALL", "dte_min": 0, "dte_max": 2,
        "dte_fallback_policy": "allow_nearest_after", "dte_fallback_max": 10,
        "min_open_interest": 100, "target_abs_delta_min": .2, "target_abs_delta_max": .5})
    def contract(name, dte, oi):
        return OptionContractSnapshot(name, "XYZ", "CALL", "2026-09-30", dte, 100, .3, 1.0, 1.05, oi)
    contracts = [contract("FIRST", 3, 0), contract("SECOND", 7, 200), contract("THIRD", 14, 1000)]
    selected = SingleLegOptionSelector().select(req, contracts)
    assert selected.option_symbol == "SECOND"
    assert selected.attempted_fallback_dtes_count == 2
    req.execution_params["dte_fallback_max"] = 5
    with pytest.raises(SelectorEmptyError):
        SingleLegOptionSelector().select(req, contracts)


def test_named_live_assignment_opens_exact_manager_gate_without_duplicate_target():
    from bhiksha.active_plan.compiler import ActivePlanSheetRow, _apply_execution_overrides, _apply_exit_overrides
    from bhiksha.config.models import ExecutionSpec
    dep = deployment()
    row = ActivePlanSheetRow(row_id="row", row_type="manual", enabled=True, symbol="QQQ",
                             authorization_mode="live", management_exit="baseline",
                             manual_setup_type="manual_trigger", direction="long", trigger_price=100, trigger_direction="ABOVE")
    execution = ExecutionSpec.model_validate(_apply_execution_overrides(dep.execution.model_dump(), row))
    exits = ExitSpec.model_validate(_apply_exit_overrides(dep.exit.model_dump(), row, exit_catalog={"baseline": profile()}))
    assert execution.runtime_mode == "live_approval_gated"
    assert exits.profile_exit_drives_live is True
    assert not exits.use_profit_target
    assert exits.compare_exits == []


def test_cartographer_named_assignment_keeps_entry_budget_provenance(tmp_path):
    from test_cartographer_profile_compiler import _row, _operator_defaults
    from bhiksha.active_plan.compiler import compile_active_plan_from_rows
    row = _row().model_copy(update={"management_exit": "baseline", "compare_exits": ["baseline"],
                                   "exit_profile_spec": None, "strategy_class": "TREND_CONTINUATION"})
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    result = compile_active_plan_from_rows(rows=[row], strategy_catalog_path=catalog,
        operator_defaults=_operator_defaults(), trading_date=row.source_metadata["trading_date"],
        exit_profiles_catalog={"baseline": profile()})
    assert result.plan.suppressed == []
    compiled = result.plan.deployments[0]
    assert compiled.exit.management_exit == "baseline"
    assert compiled.execution.shadow_only
    assert compiled.risk.max_trade_premium_usd == 400
    assert compiled.source.metadata["strategy_class"] == "TREND_CONTINUATION"
    assert compiled.source.metadata["bundle_hash"] == row.source_metadata["bundle_hash"]


def test_load_exit_profiles_sheet_rows_handles_reader_format_and_aliases():
    raw_sheet_rows = [
        {
            "row_index": 2,
            "exit_profile_id": "trend_continuation_balanced",
            "description": "The Wave Rider",
            "trade_archetype": "TREND_CONTINUATION",
            "exit_family": "staged_r_ladder",
            "target_1_r": "1",
            "target_1_fraction": "0.6",
            "target_2_r": "2",
            "initial_stop_pct": "0.3",
            "disaster_stop_pct": "0.35",
            "stop_anchor": "option_premium",
            "structural_buffer": "",
            "breakeven_after_t1": "TRUE",
            "giveback_policy": "MODERATE",
            "giveback_arm_r": "1.25",
            "giveback_retrace_fraction": "0.5",
            "risk_envelope_enabled": "FALSE",
            "no_progress_minutes": "45",
            "no_progress_min_r": "0.25",
            "max_hold_minutes": "",
            "eod_flat": "TRUE",
            "hard_flat_time_et": "15:55",
        },
        {
            "row_index": 3,
            "exit_profile_id": "flash_reversal_fast_snap",
            "description": "The Elastic Snap",
            "trade_archetype": "FLASH_REVERSAL",
            "exit_family": "time_fuse",
            "target_1_r": "1",
            "target_1_fraction": "0.75",
            "target_2_r": "1.5",
            "initial_stop_pct": "0.25",
            "disaster_stop_pct": "0.3",
            "breakeven_after_t1": "TRUE",
            "giveback_policy": "STRICT",
            "giveback_arm_r": "0.75",
            "giveback_retrace_fraction": "0.33",
            "risk_envelope_enabled": "FALSE",
            "no_progress_minutes": "15",
            "eod_flat": "TRUE",
            "hard_flat_time_et": "15:55",
        },
        {
            "row_index": 7,
            "exit_profile_id": "dynamic_envelope_curv15",
            "description": "Dynamic Net",
            "trade_archetype": "TREND_CONTINUATION",
            "exit_family": "dynamic_envelope",
            "target_1_r": "1",
            "target_1_fraction": "0.5",
            "target_2_r": "2",
            "initial_stop_pct": "0.3",
            "disaster_stop_pct": "0.35",
            "breakeven_after_t1": "TRUE",
            "giveback_policy": "MODERATE",
            "giveback_arm_r": "1.25",
            "giveback_retrace_fraction": "0.5",
            "risk_envelope_enabled": "TRUE",
            "risk_envelope_curvature": "1.5",
            "risk_envelope_activation_r": "0.5",
            "risk_envelope_initial_floor_r": "-1",
            "risk_envelope_floor_at_t1_r": "0",
            "risk_envelope_ratchet_step_r": "0.1",
            "no_progress_minutes": "45",
            "eod_flat": "TRUE",
            "hard_flat_time_et": "15:55",
        },
        {
            "row_index": 8,
            "exit_profile_id": "#commented_profile",
            "trade_archetype": "TREND_CONTINUATION",
        },
    ]
    catalog = load_exit_profiles_sheet_rows(raw_sheet_rows)
    assert len(catalog) == 3
    assert "trend_continuation_balanced" in catalog
    assert "flash_reversal_fast_snap" in catalog
    assert "dynamic_envelope_curv15" in catalog
    assert "#commented_profile" not in catalog

    tc = catalog["trend_continuation_balanced"]
    assert tc.target_1_quantity == 0.6
    assert tc.no_progress_seconds == 2700

    fr = catalog["flash_reversal_fast_snap"]
    assert fr.exit_family == "time_fuse"
    assert fr.no_progress_seconds == 900
    spec = fr.to_management_policy_spec_dict()
    assert spec["exit_family"] == "staged_r"

    dyn = catalog["dynamic_envelope_curv15"]
    assert dyn.risk_envelope_enabled is True
    assert dyn.risk_envelope_curvature == 1.5
    assert dyn.risk_envelope_activation_r == 0.5
    assert dyn.risk_envelope_initial_floor_r == -1.0


@pytest.mark.parametrize("case", ["fill", "chase_guard", "premium_cap", "fixed_concession", "disabled", "stale"])
def test_shadow_repricing_is_bounded_and_requires_a_subsequent_quote(case):
    async def run():
        dep = deployment()
        dep.execution = dep.execution.model_copy(update={
            "entry_reprice_enabled": case != "disabled", "entry_reprice_checkpoints_seconds": [2, 4],
            "entry_reprice_cancel_after_seconds": 10, "entry_reprice_spread_fractions": [1.0, 1.0],
            "entry_reprice_max_chase_pct": .15, "entry_pricing_oi_percentile_scale": False,
            "entry_execution_profile": None,
            "entry_pricing_mode": "price_seeking" if case == "fixed_concession" else "patient",
        })
        dep.risk = dep.risk.model_copy(update={"max_trade_premium_usd": 1000})
        now = datetime(2026, 9, 21, 14, tzinfo=UTC)
        decision = SignalDecision(deployment_id=dep.deployment_id, symbol=dep.symbol, timestamp=now,
                                  signal=True, direction=SignalDirection.SHORT, reason=[], features={})
        plan = TradePlan("repriced-paper", dep.deployment_id, dep.symbol, SignalDirection.SHORT,
                         "QQQ260925P00500000", 2, 2.0, ["approved"], entry_timestamp=now,
                         risk_details={"effective_max_trade_premium_usd": 400 if case == "premium_cap" else 1000})
        quote = PublicQuote(plan.option_symbol, bid=2.0, ask=2.2, open_interest=1000,
                            quote_timestamp=(now + timedelta(seconds=2)).isoformat(), quote_timestamp_field="quoteTimestamp")
        manager = SimpleNamespace(get_option_quote=AsyncMock(return_value=quote), close=AsyncMock())
        planner = SimpleNamespace(position_tracker=PositionTracker(), order_manager=manager, close=AsyncMock())
        recorder = MagicMock()
        events = SimpleNamespace(append=AsyncMock())
        supervisor = ExecutionSupervisor(planner=planner, exit_edge_recorder=recorder, event_repository=events)
        supervisor._paper_entries[plan.trade_id] = (dep, decision, plan, now, now + timedelta(seconds=10))
        supervisor.lifecycle_store.begin_entry(dep.symbol, dep.deployment_id, order_id="PAPER_PENDING")
        if case == "stale":
            quote.quote_timestamp = now.isoformat()
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=3))
        assert not recorder.prepare_registration.called  # Repricing quote cannot fill the new limit.
        if case == "premium_cap":
            assert not supervisor._paper_entries
            assert "paper_reprice_above_max_trade_premium" in str(events.append.call_args_list)
            return
        active = supervisor._paper_entries[plan.trade_id][2]
        assert active.quantity == 2
        if case in {"fixed_concession", "disabled", "stale"}:
            assert active.estimated_entry_price == 2.0
            return
        assert active.estimated_entry_price == 2.2
        assert active.risk_details["entry_pricing"]["initial_limit_price"] == 2.0
        assert "paper_entry_repriced" in str(events.append.call_args_list)
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=3.5))
        assert not recorder.prepare_registration.called  # Same quote still cannot fill.
        if case == "chase_guard":
            quote.bid, quote.ask = 2.2, 2.4
            quote.quote_timestamp = (now + timedelta(seconds=5)).isoformat()
            await supervisor.poll_paper_entries(now=now + timedelta(seconds=5))
            assert supervisor._paper_entries[plan.trade_id][2].estimated_entry_price == 2.2
            assert "paper_entry_reprice_chase_guard_resting" in str(events.append.call_args_list)
            await supervisor.poll_paper_entries(now=now + timedelta(seconds=11))
            assert not supervisor._paper_entries
            assert not recorder.prepare_registration.called
            return
        quote.quote_timestamp = (now + timedelta(seconds=4)).isoformat()
        await supervisor.poll_paper_entries(now=now + timedelta(seconds=4))
        assert recorder.prepare_registration.call_count == 1
        assert recorder.try_queue_registration.call_count == 1
        assert not supervisor._paper_entries
        assert recorder.prepare_registration.call_args.kwargs["entry_premium"] == 2.2
    asyncio.run(run())
