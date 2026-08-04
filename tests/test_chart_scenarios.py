from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mala_bhiksha_kernel import (
    ArmId,
    ArmSelection,
    ChartEvidencePacket,
    ChartScenarioSpec,
    ComponentManifest,
    ConditionType,
    EntryCondition,
    ExitProfile,
    ManagementPolicySpec,
    ObservationWindow,
    ScenarioCandidatePool,
    canonical_sha256,
    load_market_context_conformance_vectors,
)
from pydantic import ValidationError

from bhiksha.chart_scenarios import (
    BrokerInertScenarioObserver,
    BundleValidationError,
    CompletedBar,
    OptionQuoteSnapshot,
    ScenarioEventRepository,
    StaticOptionSnapshotSource,
    evaluate_condition,
    evaluate_exit_profile,
    install_shadow_plan,
    validate_bundle,
)
from bhiksha.chart_scenarios.policies import CostModel, QuoteEligibilityPolicy


def _policy(profile: ExitProfile) -> ManagementPolicySpec:
    targets = {
        ExitProfile.FLASH_REVERSAL: 0.75,
        ExitProfile.EXHAUSTION_REVERSAL: 1.0,
        ExitProfile.TREND_CONTINUATION: 2.0,
        ExitProfile.RANGE_EXPANSION: 1.5,
    }
    return ManagementPolicySpec(
        policy_id=f"fixture-{profile.value.lower()}",
        policy_schema_version="exit-policy.v1",
        stop_family="premium_pct",
        stop_anchor="filled_option_premium",
        exit_family=profile.value.lower(),
        target_model="staged_r",
        target_r=targets[profile],
        option_stop_fallback_pct=0.40,
        hard_flat_time_et="15:55",
        eod_flat=True,
    )


def _registry() -> dict[ExitProfile, ManagementPolicySpec]:
    return {profile: _policy(profile) for profile in ExitProfile}


def _cost_model(*, contracts: int = 1) -> CostModel:
    return CostModel(
        schema_version="market-context-cost-model.v1",
        contract_multiplier=100,
        contracts=contracts,
        entry_fee_per_contract_usd=1.0,
        exit_fee_per_contract_usd=1.0,
        entry_slippage_per_contract_usd=0.0,
        exit_slippage_per_contract_usd=0.0,
    )


def _quote_policy() -> QuoteEligibilityPolicy:
    return QuoteEligibilityPolicy(
        schema_version="market-context-quote-eligibility.v1",
        require_bid_ask=True,
        allow_last_fallback=False,
        max_spread_pct=0.15,
        max_quote_age_seconds=0,
        require_positive_mark=True,
    )


def _bundle_payload() -> dict:
    vectors = load_market_context_conformance_vectors()
    manifest = ComponentManifest.model_validate(vectors["component_manifest"])
    chart = ChartEvidencePacket.model_validate(vectors["chart_evidence"])
    pool = ScenarioCandidatePool.model_validate(vectors["candidate_pool"])
    selection = ArmSelection.model_validate(vectors["arm_selection"])
    deterministic_selection = selection.model_copy(
        update={"arm_id": ArmId.CHART_DETERMINISTIC, "selector_manifest_hash": "c" * 64}
    )
    scenario = ChartScenarioSpec.model_validate(vectors["chart_scenario"])
    selected_policy = _registry()[scenario.exit_profile]
    cost_model = _cost_model()
    quote_policy = _quote_policy()
    component_material = manifest.model_dump(mode="json")
    treatment_hash = "sha256:" + canonical_sha256(component_material)
    trading_date = pool.as_of.date().isoformat()
    campaign = {
        "schema": "tradelab.market_context_campaign.v1",
        "program_id": pool.program_id,
        "experiment_family_id": pool.experiment_family_id,
        "experiment_version": pool.experiment_version,
        "campaign_id": pool.campaign_id,
        "created_at": pool.as_of.isoformat().replace("+00:00", "Z"),
        "starts_on": trading_date,
        "ends_on": trading_date,
        "authorization_mode": "shadow",
        "expected_arms": ["chart_deterministic", "chart_agentic_rerank"],
        "component_manifest": component_material,
        "component_manifest_hash": treatment_hash,
        "universe_hash": "sha256:" + pool.universe_manifest_hash,
        "status": "authorized",
    }
    campaign["content_hash"] = "sha256:" + canonical_sha256(campaign)
    run = {
        "schema": "tradelab.market_context_run.v1",
        "program_id": pool.program_id,
        "experiment_family_id": pool.experiment_family_id,
        "experiment_version": pool.experiment_version,
        "campaign_id": pool.campaign_id,
        "run_id": pool.run_id,
        "trading_date": trading_date,
        "as_of": pool.as_of.isoformat().replace("+00:00", "Z"),
        "authorization_mode": "shadow",
        "expected_arms": ["chart_deterministic", "chart_agentic_rerank"],
        "input_hashes": {"candidate_pool": pool.pool_hash},
        "component_manifest_hash": treatment_hash,
        "status": "created",
    }
    run["content_hash"] = "sha256:" + canonical_sha256(run)
    scenario = scenario.model_copy(
        update={
            "management_policy": selected_policy,
            "exit_policy_id": selected_policy.policy_id,
            "exit_policy_schema_version": selected_policy.policy_schema_version,
            "exit_policy_hash": selected_policy.policy_hash,
            "cost_model_hash": cost_model.content_hash,
            "quote_eligibility_policy_hash": quote_policy.content_hash,
        }
    )
    deterministic_scenario = scenario.model_copy(
        update={
            "arm_id": deterministic_selection.arm_id,
            "scenario_id": "scenario-deterministic-1",
            "selection_packet_hash": deterministic_selection.selection_hash,
        }
    )
    return {
        "schema_version": "bhiksha.chart-scenario-shadow-plan.v1",
        "plan_id": "fixture-plan",
        "trigger_version": "market-context-trigger.v1",
        "authorization_mode": "shadow",
        "source_type": "chart_scenario_experiment",
        "campaign_manifest": campaign,
        "campaign_manifest_hash": campaign["content_hash"],
        "run_manifest": run,
        "run_manifest_hash": run["content_hash"],
        "component_manifest": manifest.model_dump(mode="json"),
        "component_manifest_hash": manifest.manifest_hash,
        "chart_evidence": [chart.model_dump(mode="json")],
        "candidate_pool": pool.model_dump(mode="json"),
        "arm_selections": [
            deterministic_selection.model_dump(mode="json"),
            selection.model_dump(mode="json"),
        ],
        "exit_policy_registry": {
            profile.value: policy.model_dump(mode="json")
            for profile, policy in _registry().items()
        },
        "cost_model": cost_model.model_dump(mode="json"),
        "quote_eligibility_policy": quote_policy.model_dump(mode="json"),
        "scenarios": [
            deterministic_scenario.model_dump(mode="json"),
            scenario.model_dump(mode="json"),
        ],
    }


def _scenario() -> ChartScenarioSpec:
    return _plan().scenarios[0]


def _plan():
    return validate_bundle(_bundle_payload())


def _observer(
    repository: ScenarioEventRepository,
    *,
    quote_source=None,
) -> BrokerInertScenarioObserver:
    plan = _plan()
    return BrokerInertScenarioObserver(
        repository,
        quote_source=quote_source,
        exit_policy_registry=plan.exit_policy_registry,
        cost_model=plan.cost_model,
        quote_eligibility_policy=plan.quote_eligibility_policy,
        plan_hash=plan.plan_hash,
    )


def _bars() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-04T10:00:00Z",
            "open": 98.0,
            "high": 99.0,
            "low": 97.0,
            "close": 99.0,
            "volume": None,
            "completed": True,
            "bar_id": None,
        },
        {
            "timestamp": "2026-08-04T10:39:00Z",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": None,
            "completed": True,
            "bar_id": None,
        },
        {
            "timestamp": "2026-08-04T11:18:00Z",
            "open": 101.0,
            "high": 103.0,
            "low": 100.0,
            "close": 102.0,
            "volume": None,
            "completed": True,
            "bar_id": None,
        },
    ]


def _quote(snapshot_id: str, mark: float, at: str) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "option_symbol": "SPY260807C00100000",
        "underlying_symbol": "SPY",
        "contract_type": "CALL",
        "expiration_date": "2026-08-07",
        "quote_time": at,
        "source_id": "fixture-read-only",
        "bid": mark - 0.05,
        "ask": mark + 0.05,
        "last": None,
        "strike": 100.0,
        "delta": 0.5,
        "open_interest": 100,
        "scenario_id": None,
        "is_selected": True,
        "provenance": {"fixture": "chart-scenario"},
        "snapshot_hash": None,
    }


def test_bundle_validation_joins_exact_hashes_and_install_is_atomic(
    tmp_path: Path,
) -> None:
    payload = _bundle_payload()
    output = tmp_path / "artifacts" / "chart_scenarios" / "active_shadow_plan.json"
    receipt = (
        tmp_path / "artifacts" / "chart_scenarios" / "active_shadow_plan.receipt.json"
    )
    live_plan = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    live_plan.parent.mkdir(parents=True)
    live_plan.write_text("live-plan-sentinel\n", encoding="utf-8")

    installed = install_shadow_plan(payload, output_path=output, receipt_path=receipt)

    assert installed["status"] == "installed"
    assert installed["broker_effect_count"] == 0
    assert installed["identities"][0]["candidate_id"] == "candidate-1"
    assert (
        installed["identities"][0]["component_manifest_hash"]
        == installed["component_manifest_hash"]
    )
    installed_bytes = output.read_bytes()
    assert (
        json.loads(installed_bytes)["schema_version"]
        == "bhiksha.chart-scenario-shadow-plan.v1"
    )
    assert live_plan.read_text(encoding="utf-8") == "live-plan-sentinel\n"
    assert not list(output.parent.glob(".active_shadow_plan.json.*.tmp"))

    bad = deepcopy(payload)
    bad["component_manifest_hash"] = "0" * 64
    with pytest.raises(BundleValidationError, match="component_manifest_hash"):
        install_shadow_plan(bad, output_path=output, receipt_path=receipt)
    assert output.read_bytes() == installed_bytes
    failure = json.loads(receipt.read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["broker_effect_count"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_mode", "live"),
        ("source_type", "manual_entry"),
        ("trigger_version", "unknown-trigger.v99"),
    ],
)
def test_bundle_rejects_non_shadow_source_or_unknown_trigger(
    field: str, value: str
) -> None:
    payload = _bundle_payload()
    payload[field] = value
    with pytest.raises(BundleValidationError):
        validate_bundle(payload)


def test_bundle_rejects_missing_hash_and_unknown_kernel_fields() -> None:
    missing = _bundle_payload()
    missing["scenarios"][0].pop("content_hash")
    with pytest.raises(BundleValidationError, match=r"scenarios\[0\].*hash"):
        validate_bundle(missing)

    unknown = _bundle_payload()
    unknown["scenarios"][0]["unknown_policy_field"] = True
    with pytest.raises(BundleValidationError):
        validate_bundle(unknown)

    bad_profile = deepcopy(_bundle_payload()["scenarios"][0])
    bad_profile.pop("content_hash")
    bad_profile["exit_profile"] = "UNSUPPORTED_PROFILE"
    with pytest.raises(ValidationError, match="exit_profile|Input should be"):
        ChartScenarioSpec.model_validate(bad_profile)


def test_bundle_rejects_missing_or_mismatched_exit_policy_registry() -> None:
    missing = _bundle_payload()
    missing["exit_policy_registry"].pop(ExitProfile.RANGE_EXPANSION.value)
    with pytest.raises(BundleValidationError, match="missing compatible profiles"):
        validate_bundle(missing)

    mismatched = _bundle_payload()
    mismatched["exit_policy_registry"][ExitProfile.TREND_CONTINUATION.value] = _policy(
        ExitProfile.RANGE_EXPANSION
    ).model_dump(mode="json")
    with pytest.raises(
        BundleValidationError, match="unsupported exit_family|selected policy differs"
    ):
        validate_bundle(mismatched)

    absent_selected_material = _bundle_payload()
    raw_scenario = ChartScenarioSpec.model_validate(
        load_market_context_conformance_vectors()["chart_scenario"]
    ).model_copy(
        update={
            "cost_model_hash": _cost_model().content_hash,
            "quote_eligibility_policy_hash": _quote_policy().content_hash,
        }
    )
    absent_selected_material["scenarios"] = [raw_scenario.model_dump(mode="json")]
    with pytest.raises(
        BundleValidationError, match="missing selected management_policy"
    ):
        validate_bundle(absent_selected_material)

    omitted_default = _bundle_payload()
    omitted_default["exit_policy_registry"][ExitProfile.FLASH_REVERSAL.value].pop(
        "max_hold_seconds"
    )
    with pytest.raises(
        BundleValidationError, match="explicitly declare every policy field"
    ):
        validate_bundle(omitted_default)

    unsupported = _bundle_payload()
    unsupported["exit_policy_registry"][ExitProfile.FLASH_REVERSAL.value][
        "risk_envelope_enabled"
    ] = True
    unsupported["exit_policy_registry"][ExitProfile.FLASH_REVERSAL.value].update(
        {
            "risk_envelope_activation_r": 0.25,
            "risk_envelope_initial_floor_r": -1.0,
            "risk_envelope_curvature": 1.5,
            "risk_envelope_floor_at_t1_r": 0.0,
            "risk_envelope_ratchet_step_r": 0.1,
            "target_1_r": 1.0,
            "target_2_r": 2.0,
            "target_1_quantity": 0.75,
        }
    )
    with pytest.raises(
        BundleValidationError, match="unsupported risk-envelope semantics"
    ):
        validate_bundle(unsupported)

    unsupported_family = _bundle_payload()
    unsupported_family["exit_policy_registry"][ExitProfile.FLASH_REVERSAL.value][
        "stop_family"
    ] = "underlying_atr"
    with pytest.raises(BundleValidationError, match="unsupported stop_family"):
        validate_bundle(unsupported_family)


def test_bundle_refuses_live_active_plan_paths_without_reading_them(
    tmp_path: Path,
) -> None:
    live = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"secret": "sentinel"}\n', encoding="utf-8")
    with pytest.raises(BundleValidationError, match="live active-plan path"):
        install_shadow_plan(
            live,
            output_path=tmp_path / "artifacts" / "chart_scenarios" / "shadow.json",
        )
    assert live.read_text(encoding="utf-8") == '{"secret": "sentinel"}\n'

    arbitrary = tmp_path / "config" / "runtime.json"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("runtime-sentinel\n", encoding="utf-8")
    with pytest.raises(BundleValidationError, match="artifacts/chart_scenarios"):
        install_shadow_plan(_bundle_payload(), output_path=arbitrary)
    assert arbitrary.read_text(encoding="utf-8") == "runtime-sentinel\n"

    with pytest.raises(ValueError, match="artifacts/chart_scenarios"):
        ScenarioEventRepository(
            tmp_path / "artifacts" / "playbook" / "active_plan.json"
        )

    same = tmp_path / "artifacts" / "chart_scenarios" / "same.json"
    with pytest.raises(BundleValidationError, match="paths must differ"):
        install_shadow_plan(_bundle_payload(), output_path=same, receipt_path=same)


def test_raw_observations_are_exact_and_deeply_immutable() -> None:
    quote = _quote("q-exact", 1.0, "2026-08-04T11:20:00Z")
    quote["provenance"] = {"nested": {"source": "fixture"}}
    snapshot = OptionQuoteSnapshot.from_mapping(quote)
    quote["provenance"]["nested"]["source"] = "mutated"  # type: ignore[index]
    assert snapshot.to_dict()["provenance"]["nested"]["source"] == "fixture"

    missing_quote = _quote("q-missing", 1.0, "2026-08-04T11:20:00Z")
    missing_quote.pop("source_id")
    with pytest.raises(ValueError, match="exact fields"):
        OptionQuoteSnapshot.from_mapping(missing_quote)
    missing_bar = _bars()[0]
    missing_bar.pop("completed")
    with pytest.raises(ValueError, match="exact fields"):
        CompletedBar.from_mapping(missing_bar)
    with pytest.raises(ValidationError, match="frozen"):
        _cost_model().exit_fee_per_contract_usd = 99.0
    with pytest.raises(ValidationError, match="frozen"):
        _quote_policy().max_quote_age_seconds = 999


def test_snapshot_source_is_data_only_and_cannot_be_monkeypatched() -> None:
    source = StaticOptionSnapshotSource(
        [_quote("q-source", 1.0, "2026-08-04T11:20:00Z")]
    )
    with pytest.raises((AttributeError, TypeError)):
        source.get_snapshot = lambda **_: None  # type: ignore[method-assign]


def test_stale_single_quote_and_treatment_drift_fail_closed(tmp_path: Path) -> None:
    plan = _plan()
    scenario = plan.scenarios[0]
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "stale.sqlite3"
    )
    stale = _observer(repository).observe_one(
        scenario,
        bars=_bars(),
        option_quote=_quote("q-stale", 1.0, "2026-08-04T11:20:00Z"),
        evaluated_at="2026-08-04T11:30:00Z",
        market_observation_id="caller-label-a",
    )
    assert not any(
        event.event_type.value == "synthetic_entry" for event in stale.events
    )
    assert any(event.event_type.value == "quote_unavailable" for event in stale.events)

    changed_cost = plan.cost_model.model_copy(
        update={"exit_fee_per_contract_usd": 2.0, "content_hash": None}
    )
    changed_cost = CostModel.model_validate(changed_cost.model_dump(mode="json"))
    drifted = BrokerInertScenarioObserver(
        ScenarioEventRepository(
            tmp_path / "artifacts" / "chart_scenarios" / "drift.sqlite3"
        ),
        exit_policy_registry=plan.exit_policy_registry,
        cost_model=changed_cost,
        quote_eligibility_policy=plan.quote_eligibility_policy,
        plan_hash=plan.plan_hash,
    ).observe_one(
        scenario,
        bars=_bars(),
        market_observation_id="caller-label-b",
    )
    assert drifted.error is not None
    assert "cost model differs" in drifted.error


def test_typed_trigger_primitives_require_completed_bars_and_respect_expiry() -> None:
    window = ObservationWindow(
        start_at="2026-08-04T10:00:00Z",
        end_at="2026-08-04T12:00:00Z",
        market_timezone="America/New_York",
    )
    bars = [
        CompletedBar(datetime(2026, 8, 4, 10, 0, tzinfo=UTC), 98, 99, 97, 99),
        CompletedBar(datetime(2026, 8, 4, 10, 39, tzinfo=UTC), 100, 102, 99, 101),
        CompletedBar(datetime(2026, 8, 4, 11, 18, tzinfo=UTC), 101, 103, 100, 102),
    ]

    def condition(kind: ConditionType, **kwargs: object) -> EntryCondition:
        return EntryCondition(condition_type=kind, timeframe="39m", **kwargs)

    cross = evaluate_condition(
        condition(ConditionType.CROSS_ABOVE, level=100, level_ref="chart#level"),
        bars,
        window,
    )
    assert cross.triggered
    hold = evaluate_condition(
        condition(ConditionType.HOLD_ABOVE, level=100, level_ref="chart#level", bars=2),
        bars,
        window,
    )
    assert hold.triggered
    bars_below = bars + [
        CompletedBar(datetime(2026, 8, 4, 11, 57, tzinfo=UTC), 101, 102, 98, 99),
    ]
    below = evaluate_condition(
        condition(ConditionType.CROSS_BELOW, level=100, level_ref="chart#level"),
        bars_below,
        window,
    )
    assert below.triggered
    hold_below = evaluate_condition(
        condition(ConditionType.HOLD_BELOW, level=105, level_ref="chart#level", bars=2),
        bars,
        window,
    )
    assert hold_below.triggered
    reclaim = evaluate_condition(
        condition(
            ConditionType.RECLAIM,
            level=100,
            level_ref="chart#level",
            window_seconds=3600,
        ),
        bars,
        window,
    )
    assert reclaim.triggered
    reject = evaluate_condition(
        condition(
            ConditionType.REJECT,
            level=100,
            level_ref="chart#level",
            window_seconds=3600,
        ),
        bars,
        window,
    )
    assert not reject.triggered
    breakout = evaluate_condition(
        condition(
            ConditionType.RANGE_BREAKOUT,
            range_low=90,
            range_high=100,
            range_low_ref="chart#low",
            range_high_ref="chart#high",
            buffer=1,
            direction="long",
        ),
        bars,
        window,
    )
    assert breakout.triggered
    short_breakout = evaluate_condition(
        condition(
            ConditionType.RANGE_BREAKOUT,
            range_low=101,
            range_high=110,
            range_low_ref="chart#low",
            range_high_ref="chart#high",
            buffer=1,
            direction="short",
        ),
        bars_below,
        window,
    )
    assert short_breakout.triggered
    expired = evaluate_condition(
        condition(ConditionType.CROSS_ABOVE, level=100, level_ref="chart#level"),
        bars,
        window,
        evaluated_at="2026-08-04T12:01:00Z",
    )
    assert not expired.triggered
    assert expired.reason == "observation_window_expired"
    with pytest.raises(ValueError, match="completed bars"):
        evaluate_condition(
            condition(ConditionType.CROSS_ABOVE, level=100, level_ref="chart#level"),
            [{**_bars()[0], "completed": False}],
            window,
        )


def test_staged_exit_reports_weighted_gross_and_after_cost_r() -> None:
    policy = _policy(ExitProfile.TREND_CONTINUATION).model_copy(
        update={
            "target_1_r": 1.0,
            "target_2_r": 2.0,
            "target_1_quantity": 0.5,
            "breakeven_after_t1": True,
        }
    )
    entry = OptionQuoteSnapshot.from_mapping(
        _quote("q-entry", 1.0, "2026-08-04T14:00:00Z")
    )
    first_target = OptionQuoteSnapshot.from_mapping(
        _quote("q-target-1", 1.4, "2026-08-04T14:10:00Z")
    )
    staged_cost_model = _cost_model(contracts=10)
    partial = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        entry,
        first_target,
        entry_time=entry.quote_time,
        evaluated_at=first_target.quote_time,
        management_policy=policy,
        cost_model=staged_cost_model,
        quote_eligibility_policy=_quote_policy(),
    )
    assert partial.status == "partial"
    assert partial.gross_r == pytest.approx(1.0)
    assert partial.net_r == pytest.approx(0.95)
    assert partial.state["realized_r_contracts"] == pytest.approx(5.0)
    assert partial.state["remaining_contracts"] == 5

    final_quote = OptionQuoteSnapshot.from_mapping(
        _quote("q-target-2", 1.8, "2026-08-04T14:20:00Z")
    )
    final = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        entry,
        final_quote,
        entry_time=entry.quote_time,
        evaluated_at=final_quote.quote_time,
        management_policy=policy,
        cost_model=staged_cost_model,
        quote_eligibility_policy=_quote_policy(),
        prior_state=partial.state,
    )
    assert final.rule == "target_2"
    assert final.r == pytest.approx(2.0)
    assert final.gross_r == pytest.approx(1.5)
    assert final.net_r == pytest.approx(1.45)

    breakeven_quote = OptionQuoteSnapshot.from_mapping(
        _quote("q-breakeven", 1.0, "2026-08-04T14:15:00Z")
    )
    breakeven = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        entry,
        breakeven_quote,
        entry_time=entry.quote_time,
        evaluated_at=breakeven_quote.quote_time,
        management_policy=policy,
        cost_model=staged_cost_model,
        quote_eligibility_policy=_quote_policy(),
        prior_state=partial.state,
    )
    assert breakeven.rule == "breakeven_after_target_1"
    assert breakeven.gross_r == pytest.approx(0.5)
    assert breakeven.net_r == pytest.approx(0.45)

    one_contract = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        entry,
        final_quote,
        entry_time=entry.quote_time,
        evaluated_at=final_quote.quote_time,
        management_policy=policy,
        cost_model=_cost_model(contracts=1),
        quote_eligibility_policy=_quote_policy(),
    )
    assert one_contract.rule == "target_1"
    assert one_contract.is_terminal


def test_no_progress_exit_uses_explicit_favorable_floor() -> None:
    policy = _policy(ExitProfile.EXHAUSTION_REVERSAL).model_copy(
        update={
            "no_progress_seconds": 300,
            "parameters": {"no_progress_favorable_floor_r": 0.25},
        }
    )
    entry = OptionQuoteSnapshot.from_mapping(
        _quote("q-entry", 1.0, "2026-08-04T14:00:00Z")
    )
    stalled = OptionQuoteSnapshot.from_mapping(
        _quote("q-stalled", 1.04, "2026-08-04T14:06:00Z")
    )
    result = evaluate_exit_profile(
        ExitProfile.EXHAUSTION_REVERSAL,
        entry,
        stalled,
        entry_time=entry.quote_time,
        evaluated_at=stalled.quote_time,
        management_policy=policy,
        cost_model=_cost_model(),
        quote_eligibility_policy=_quote_policy(),
    )
    assert result.rule == "no_progress"
    assert result.is_terminal


def test_observer_is_restart_safe_terminal_and_emits_primary_and_counterfactual_marks(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    )
    observer = _observer(repository)
    quotes = [
        _quote("q-entry", 1.05, "2026-08-04T11:20:00Z"),
        _quote("q-range", 1.85, "2026-08-04T11:40:00Z"),
        # Exactly 2R under the selected 40% stop must not miss on binary
        # floating-point representation.
        _quote("q-trend", 1.89, "2026-08-04T11:50:00Z"),
    ]

    first = observer.observe_one(
        scenario,
        bars=_bars(),
        quote_path=quotes,
        market_observation_id="observation-1",
    )
    assert first.error is None
    assert first.terminal
    assert first.broker_effect_count == 0
    event_types = [event.event_type.value for event in repository.events()]
    assert "synthetic_entry" in event_types
    assert "synthetic_exit" in event_types
    exit_events = [
        event
        for event in repository.events()
        if event.event_type.value == "exit_observation"
    ]
    assert {event.details["profile"] for event in exit_events} == {
        "TREND_CONTINUATION",
        "RANGE_EXPANSION",
    }
    assert (
        sum(event.details["profile"] == "RANGE_EXPANSION" for event in exit_events) == 1
    )
    assert any(event.details["counterfactual"] for event in exit_events)
    assert all(event.details["evaluated_exit_policy_hash"] for event in exit_events)
    assert (
        len({event.details["evaluated_exit_policy_hash"] for event in exit_events}) == 2
    )
    assert all(event.broker_effect_count == 0 for event in repository.events())
    assert all(event.details["mark_not_fill"] for event in exit_events)
    assert repository.verify_event_chain().valid

    count = repository.event_count()
    replay = observer.observe_one(
        scenario,
        bars=_bars(),
        quote_path=quotes,
        market_observation_id="observation-1",
    )
    assert replay.terminal
    assert replay.new_events == ()
    assert repository.event_count() == count

    rearm = observer.observe_one(
        scenario,
        bars=_bars(),
        option_quote=_quote("q-after-terminal", 3.0, "2026-08-04T11:55:00Z"),
        market_observation_id="observation-2",
    )
    assert rearm.terminal
    assert repository.event_count() == count


def test_quote_unavailable_then_recovery_does_not_duplicate_trigger(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    )
    observer = _observer(repository)
    first = observer.observe_one(
        scenario, bars=_bars(), market_observation_id="missing-quote"
    )
    assert not first.terminal
    assert "quote_unavailable" in [event.event_type.value for event in first.events]
    trigger_count = sum(
        event.event_type.value == "entry_triggered" for event in repository.events()
    )
    second = observer.observe_one(
        scenario,
        bars=_bars(),
        quote_path=[
            _quote("q-entry", 1.05, "2026-08-04T11:20:00Z"),
            _quote("q-exit", 2.05, "2026-08-04T11:40:00Z"),
        ],
        market_observation_id="quote-recovered",
    )
    assert second.error is None
    assert (
        sum(
            event.event_type.value == "entry_triggered" for event in repository.events()
        )
        == trigger_count
    )
    assert any(
        event.event_type.value == "synthetic_entry" for event in repository.events()
    )
    assert repository.verify_event_chain().valid


def test_repository_detects_event_chain_tampering(tmp_path: Path) -> None:
    scenario = _scenario()
    db_path = tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    repository = ScenarioEventRepository(db_path)
    _observer(repository).observe_one(
        scenario,
        bars=_bars(),
        quote_path=[_quote("q-entry", 1.05, "2026-08-04T11:20:00Z")],
        market_observation_id="chain-observation",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE chart_scenario_events SET event_hash=? WHERE sequence=2",
            ("0" * 64,),
        )
        conn.commit()
    report = repository.verify_event_chain()
    assert not report.valid
    assert report.errors


def test_read_only_quote_source_rejects_order_capability(tmp_path: Path) -> None:
    class NotActuallyReadOnly:
        def get_snapshot(self, *, scenario: object, at: datetime) -> None:
            return None

        def submit_order(self, *args: object, **kwargs: object) -> None:
            return None

    with pytest.raises(TypeError, match="prohibited order capability"):
        BrokerInertScenarioObserver(
            ScenarioEventRepository(
                tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
            ),
            quote_source=NotActuallyReadOnly(),
            exit_policy_registry=_registry(),
            cost_model=_cost_model(),
            quote_eligibility_policy=_quote_policy(),
            plan_hash=_plan().plan_hash,
        )

    hidden_effects: list[str] = []

    class HiddenCallback:
        def get_snapshot(self, *, scenario: object, at: datetime) -> None:
            hidden_effects.append("effect")

    with pytest.raises(TypeError, match="sealed data-only"):
        _observer(
            ScenarioEventRepository(
                tmp_path / "artifacts" / "chart_scenarios" / "hidden.sqlite3"
            ),
            quote_source=HiddenCallback(),
        )
    assert hidden_effects == []


def test_restart_rejects_counterfactual_policy_or_plan_drift(tmp_path: Path) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    )
    _observer(repository).observe_one(
        plan.scenarios[0],
        bars=_bars(),
        market_observation_id="first-plan-observation",
    )

    changed_payload = _bundle_payload()
    changed_policy = _policy(ExitProfile.RANGE_EXPANSION).model_copy(
        update={"target_r": 1.75}
    )
    changed_payload["exit_policy_registry"][ExitProfile.RANGE_EXPANSION.value] = (
        changed_policy.model_dump(mode="json")
    )
    changed_plan = validate_bundle(changed_payload)
    changed_observer = BrokerInertScenarioObserver(
        repository,
        exit_policy_registry=changed_plan.exit_policy_registry,
        cost_model=changed_plan.cost_model,
        quote_eligibility_policy=changed_plan.quote_eligibility_policy,
        plan_hash=changed_plan.plan_hash,
    )
    changed_result = changed_observer.observe_one(
        changed_plan.scenarios[0],
        bars=_bars(),
        market_observation_id="second-plan-observation",
    )
    assert changed_result.error is not None
    assert "different shadow plan hash" in changed_result.error


def test_shared_candidate_arms_require_identical_market_facts(tmp_path: Path) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    )
    observer = _observer(repository)
    observer.observe_one(
        plan.scenarios[0],
        bars=_bars(),
        market_observation_id="paired-arm-observation",
    )
    changed_bars = deepcopy(_bars())
    changed_bars[-1]["close"] = 101.5
    changed_result = observer.observe_one(
        plan.scenarios[1],
        bars=changed_bars,
        market_observation_id="different-caller-label",
    )
    assert changed_result.error is not None
    assert "different market facts" in changed_result.error


def test_restart_recovers_latched_primary_terminal_after_crash(tmp_path: Path) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "crash.sqlite3"
    )
    observer = _observer(repository)
    original = observer._record_terminal

    def crash_before_synthetic_exit(*args: object, **kwargs: object):
        event_type = args[2]
        if getattr(event_type, "value", event_type) == "synthetic_exit":
            raise SystemExit("simulated process death")
        return original(*args, **kwargs)

    observer._record_terminal = crash_before_synthetic_exit  # type: ignore[method-assign]
    quotes = [
        _quote("q-entry", 1.0, "2026-08-04T11:20:00Z"),
        _quote("q-exit", 2.0, "2026-08-04T11:40:00Z"),
    ]
    with pytest.raises(SystemExit, match="simulated process death"):
        observer.observe_one(
            scenario,
            bars=_bars(),
            quote_path=quotes,
            market_observation_id="crash-cycle",
        )
    stranded = repository.get_state(scenario, observer.trigger_version)
    assert stranded is not None and not stranded["terminal"]
    assert stranded["profile_states"][scenario.exit_profile.value]["terminal"]

    recovered = _observer(repository).observe_one(
        scenario,
        bars=_bars(),
        quote_path=quotes,
        market_observation_id="renamed-crash-cycle",
    )
    assert recovered.error is None
    assert recovered.terminal
    assert recovered.status == "synthetic_exit"


def test_new_package_import_graph_has_no_money_path_imports() -> None:
    root = Path(__file__).parents[1] / "src" / "bhiksha" / "chart_scenarios"
    prohibited_prefixes = (
        "bhiksha.execution",
        "bhiksha.active_plan",
        "bhiksha.state.reconciliation",
        "bhiksha.app.runtime",
    )
    prohibited_functions = {
        "place_order",
        "submit_order",
        "cancel_order",
        "replace_order",
        "reconcile",
        "square_off",
        "close_position",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in names
                for prefix in prohibited_prefixes
            ), path
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in prohibited_functions, (path, node.name)


def test_cli_install_observe_and_status_are_read_only_fixture_commands(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    kernel = root.parent / "kernel-market-context"
    env = {
        **dict(__import__("os").environ),
        "PYTHONPATH": f"{root / 'src'}:{kernel / 'src'}",
    }
    bundle_path = tmp_path / "bundle.json"
    plan_path = tmp_path / "artifacts" / "chart_scenarios" / "active_shadow_plan.json"
    receipt_path = tmp_path / "artifacts" / "chart_scenarios" / "receipt.json"
    db_path = tmp_path / "artifacts" / "chart_scenarios" / "events.sqlite3"
    bundle_path.write_text(json.dumps(_bundle_payload()), encoding="utf-8")
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "bhiksha.chart_scenarios",
            "install",
            "--input",
            str(bundle_path),
            "--output",
            str(plan_path),
            "--receipt",
            str(receipt_path),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr + install.stdout
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "plan": _bundle_payload(),
                "bars": _bars(),
                "quotes": [_quote("q1", 1.05, "2026-08-04T11:20:00Z")],
            }
        ),
        encoding="utf-8",
    )
    observe = subprocess.run(
        [
            sys.executable,
            "-m",
            "bhiksha.chart_scenarios",
            "observe-one",
            "--fixture",
            str(fixture_path),
            "--db-path",
            str(db_path),
            "--observation-id",
            "cli-fixture-observation",
            "--scenario-id",
            "scenario-deterministic-1",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert observe.returncode == 0, observe.stderr + observe.stdout
    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "bhiksha.chart_scenarios",
            "status",
            "--plan",
            str(plan_path),
            "--db-path",
            str(db_path),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr + status.stdout
    status_payload = json.loads(status.stdout)
    assert status_payload["event_chain_valid"]
    assert status_payload["broker_effect_count"] == 0
