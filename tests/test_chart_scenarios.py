from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, time
from pathlib import Path

import httpx
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
    run_artifact_paths,
    run_observation_cycle,
    validate_bundle,
)
from bhiksha.chart_scenarios.policies import (
    CostModel,
    OptionSelectionPolicy,
    QuoteEligibilityPolicy,
)
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import Bar, OptionContractSnapshot, OptionSelectionRequest
from bhiksha.execution.profile_exit import (
    ProfileExitFields,
    ProfileExitState,
    ProfileMarketView,
    evaluate_profile_exit,
)
from bhiksha.ops.chart_scenario_live_export import export_live_cycle_input
from bhiksha.options.selectors import SingleLegOptionSelector


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


def _option_selection_policy() -> OptionSelectionPolicy:
    return OptionSelectionPolicy(
        schema_version="bhiksha.chart-scenario-option-selection-policy.v1",
        provider_id="schwab",
        long_signal_contract_type="CALL",
        short_signal_contract_type="PUT",
        dte_min=0,
        dte_max=7,
        target_abs_delta_min=0.25,
        target_abs_delta_max=0.55,
        min_open_interest=50,
        max_bid_ask_spread_pct=0.15,
        dte_fallback_policy="allow_nearest_after",
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
    registry = _registry()
    selected_policy = registry[scenario.exit_profile]
    cost_model = _cost_model()
    quote_policy = _quote_policy()
    option_selection_policy = _option_selection_policy()
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
    registry_material = {
        profile.value: policy.model_dump(mode="json")
        for profile, policy in registry.items()
    }
    frozen_material = {
        "exit_policy_registry": registry_material,
        "cost_model": cost_model.model_dump(mode="json"),
        "quote_eligibility_policy": quote_policy.model_dump(mode="json"),
        "option_selection_policy": option_selection_policy.model_dump(mode="json"),
        "selected_profile_by_thesis": {
            scenario.thesis_class.value: scenario.exit_profile.value
        },
    }
    frozen_behavior = {
        name: {
            "material": material,
            "content_hash": "sha256:" + canonical_sha256(material),
        }
        for name, material in frozen_material.items()
    }
    ranker_manifest = {
        "provider_id": "fixture",
        "model_id": "fixture",
        "prompt_contract_path": "fixture",
        "prompt_contract_sha256": "a" * 64,
        "maximum": 10,
    }
    treatment_body = {
        "schema": "tradelab.market_context_treatment.v1",
        "program_id": pool.program_id,
        "experiment_family_id": pool.experiment_family_id,
        "experiment_version": pool.experiment_version,
        "treatment_version": "fixture-v1",
        "component_commits": {
            name: "a" * 40 for name in ("kernel", "cartographer", "tradelab", "bhiksha")
        },
        "ranker": ranker_manifest,
        "frozen_behavior": frozen_behavior,
        "narrative": {
            "mode": "observational_sidecar",
            "selection_influence": False,
            "included_in_treatment": False,
        },
        "daily_cartographer_inputs_or_outputs": "excluded",
    }
    treatment = {
        **treatment_body,
        "content_hash": "sha256:" + canonical_sha256(treatment_body),
    }
    treatment_hash = treatment["content_hash"]
    trading_date = pool.as_of.date().isoformat()
    campaign = {
        "schema": "tradelab.market_context_campaign.v2",
        "program_id": pool.program_id,
        "experiment_family_id": pool.experiment_family_id,
        "experiment_version": pool.experiment_version,
        "campaign_id": pool.campaign_id,
        "created_at": pool.as_of.isoformat().replace("+00:00", "Z"),
        "starts_on": trading_date,
        "ends_on": trading_date,
        "authorization_mode": "shadow",
        "expected_arms": ["chart_deterministic", "chart_agentic_rerank"],
        "treatment_manifest": treatment,
        "treatment_manifest_hash": treatment_hash,
        "universe_hash": "sha256:" + pool.universe_manifest_hash,
        "status": "authorized",
    }
    campaign["content_hash"] = "sha256:" + canonical_sha256(campaign)
    target_window = pool.candidates[0].observation_window.model_dump(mode="json")
    window_hash = canonical_sha256(target_window)
    export_body = {
        "schema": "market_cartographer.market_context_export.v2",
        "campaign_id": pool.campaign_id,
        "run_id": pool.run_id,
        "target_session_date": trading_date,
        "target_session_window": target_window,
        "target_session_window_hash": window_hash,
        "candidate_pool_hash": pool.pool_hash,
    }
    export_hash = canonical_sha256(export_body)
    export_manifest = {
        **export_body,
        "export_id": f"export:{export_hash[:16]}",
        "export_hash": export_hash,
    }
    receipt_body = {
        "schema": "market_cartographer.market_context_receipt.v2",
        "status": "succeeded",
        "run_id": pool.run_id,
        "export_hash": export_hash,
        "target_session_date": trading_date,
        "target_session_window": target_window,
        "target_session_window_hash": window_hash,
        "candidate_pool_hash": pool.pool_hash,
    }
    receipt_hash = canonical_sha256(receipt_body)
    receipt = {**receipt_body, "receipt_hash": receipt_hash}
    selector_body = {
        "schema": "tradelab.market_context_ranker_receipt.v1",
        "status": "succeeded",
        "execution_mode": "deterministic_plumbing_canary",
        "campaign_id": pool.campaign_id,
        "run_id": pool.run_id,
        "candidate_pool_hash": pool.pool_hash,
        "selection_hash": selection.selection_hash,
        "provider_id": ranker_manifest["provider_id"],
        "model_id": ranker_manifest["model_id"],
        "prompt_contract_sha256": ranker_manifest["prompt_contract_sha256"],
        "maximum": ranker_manifest["maximum"],
        "agent_broker_receipt": None,
        "agent_broker_receipt_hash": None,
        "run_comparable": False,
        "non_comparable_reason": "deterministic_plumbing_canary",
    }
    selector_receipt = {
        **selector_body,
        "content_hash": "sha256:" + canonical_sha256(selector_body),
    }
    run = {
        "schema": "tradelab.market_context_run.v2",
        "program_id": pool.program_id,
        "experiment_family_id": pool.experiment_family_id,
        "experiment_version": pool.experiment_version,
        "campaign_id": pool.campaign_id,
        "run_id": pool.run_id,
        "trading_date": trading_date,
        "target_session_date": trading_date,
        "as_of": pool.as_of.isoformat().replace("+00:00", "Z"),
        "authorization_mode": "shadow",
        "expected_arms": ["chart_deterministic", "chart_agentic_rerank"],
        "input_hashes": {"candidate_pool": "sha256:" + pool.pool_hash},
        "treatment_manifest_hash": treatment_hash,
        "cartographer_receipt_hash": receipt_hash,
        "cartographer_export_hash": export_hash,
        "observation_window_hash": window_hash,
        "status": "created",
    }
    run["content_hash"] = "sha256:" + canonical_sha256(run)
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
        "treatment_manifest": treatment,
        "treatment_manifest_hash": treatment_hash,
        "cartographer_receipt": receipt,
        "cartographer_receipt_hash": receipt_hash,
        "cartographer_export_manifest": export_manifest,
        "cartographer_export_hash": export_hash,
        "target_session_date": trading_date,
        "target_session_window_hash": window_hash,
        "arm_b_selector_receipt": selector_receipt,
        "arm_b_selector_receipt_hash": selector_receipt["content_hash"],
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
            for profile, policy in registry.items()
        },
        "cost_model": cost_model.model_dump(mode="json"),
        "quote_eligibility_policy": quote_policy.model_dump(mode="json"),
        "option_selection_policy": option_selection_policy.model_dump(mode="json"),
        "scenarios": [
            deterministic_scenario.model_dump(mode="json"),
            scenario.model_dump(mode="json"),
        ],
    }


def _scenario() -> ChartScenarioSpec:
    return _plan().scenarios[0]


def _plan():
    return validate_bundle(_bundle_payload())


def _cycle_input(plan=None, *, slot: int = 1) -> dict:
    active_plan = plan or _plan()
    candidates = []
    seen: set[str] = set()
    for scenario in active_plan.scenarios:
        if scenario.candidate_id in seen:
            continue
        seen.add(scenario.candidate_id)
        candidates.append(
            {
                "candidate_id": scenario.candidate_id,
                "symbol": scenario.symbol,
                "bars": _bars(),
                "quotes": [],
                "diagnostics": {"comparable": True, "errors": []},
            }
        )
    body = {
        "schema_version": "bhiksha.chart-scenario-cycle-input.v1",
        "plan_hash": active_plan.plan_hash,
        "run_manifest_hash": active_plan.run_manifest_hash,
        "treatment_manifest_hash": active_plan.treatment_manifest_hash,
        "observation_slot_ordinal": slot,
        "evaluated_at": "2026-08-04T11:18:00Z",
        "candidates": candidates,
    }
    return {**body, "content_hash": canonical_sha256(body)}


def _observer(
    repository: ScenarioEventRepository,
    *,
    quote_source=None,
) -> BrokerInertScenarioObserver:
    plan = _plan()
    return BrokerInertScenarioObserver(
        repository,
        plan=plan,
        quote_source=quote_source,
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
    assert (
        installed["receipt_schema_version"]
        == "bhiksha.chart-scenario-install-receipt.v2"
    )
    assert installed["receipt_hash"] == canonical_sha256(
        {key: value for key, value in installed.items() if key != "receipt_hash"}
    )
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
    assert failure["receipt_hash"] == canonical_sha256(
        {key: value for key, value in failure.items() if key != "receipt_hash"}
    )


def test_run_artifact_paths_never_pool_two_daily_event_chains(tmp_path: Path) -> None:
    root = tmp_path / "artifacts" / "chart_scenarios" / "runs"
    first = run_artifact_paths("campaign-1", "run-day-1", root=root)
    second = run_artifact_paths("campaign-1", "run-day-2", root=root)

    assert first.database != second.database
    assert first.cycle_receipts.parent == first.root
    assert second.cycle_receipts.parent == second.root
    assert ScenarioEventRepository(first.database).events() == []
    assert ScenarioEventRepository(second.database).events() == []


def test_frozen_option_policy_drives_canonical_short_put_selection() -> None:
    policy = _option_selection_policy()
    request = OptionSelectionRequest(
        deployment_id="chart-scenario:candidate-short",
        symbol="SPY",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 8, 4, 14, 0, tzinfo=UTC),
        execution_profile="chart_scenario_shadow_v1",
        execution_params=policy.selector_params(),
    )
    contracts = [
        OptionContractSnapshot(
            option_symbol="SPY260807C00100000",
            underlying_symbol="SPY",
            contract_type="CALL",
            expiration_date="2026-08-07",
            dte=3,
            strike=100.0,
            delta=0.40,
            bid=1.0,
            ask=1.1,
            open_interest=100,
        ),
        OptionContractSnapshot(
            option_symbol="SPY260807P00100000",
            underlying_symbol="SPY",
            contract_type="PUT",
            expiration_date="2026-08-07",
            dte=3,
            strike=100.0,
            delta=-0.40,
            bid=1.0,
            ask=1.1,
            open_interest=100,
        ),
    ]

    selected = SingleLegOptionSelector().select(request, contracts)

    assert selected.option_symbol == "SPY260807P00100000"


def test_live_export_uses_completed_schwab_bars_and_canonical_selector(
    tmp_path: Path,
) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "live-export.sqlite3"
    )

    class _Bars:
        async def warm_start(self, symbol, start, end):
            del start, end
            return [
                Bar(
                    symbol,
                    datetime(2026, 8, 4, 11, 17, tzinfo=UTC),
                    99,
                    101,
                    98,
                    100,
                    10,
                ),
                Bar(
                    symbol,
                    datetime(2026, 8, 4, 11, 18, tzinfo=UTC),
                    100,
                    102,
                    99,
                    101,
                    11,
                ),
            ]

    class _Chain:
        async def get_chain(self, symbol, **kwargs):
            assert kwargs["contract_type"] == "CALL"
            return [
                OptionContractSnapshot(
                    option_symbol="SPY260807C00100000",
                    underlying_symbol=symbol,
                    contract_type="CALL",
                    expiration_date="2026-08-07",
                    dte=3,
                    strike=100.0,
                    delta=0.4,
                    bid=1.0,
                    ask=1.1,
                    open_interest=100,
                )
            ]

    class _QuoteClient:
        async def quote(self, option_symbol):
            return {
                option_symbol: {
                    "quote": {
                        "bidPrice": 1.0,
                        "askPrice": 1.1,
                        "lastPrice": 1.05,
                        "quoteTime": int(
                            datetime(2026, 8, 4, 11, 18, 30, tzinfo=UTC).timestamp()
                            * 1000
                        ),
                    },
                    "reference": {
                        "underlyingSymbol": "SPY",
                        "contractType": "CALL",
                        "expirationDate": "2026-08-07",
                        "strikePrice": 100.0,
                    },
                }
            }

    payload = asyncio.run(
        export_live_cycle_input(
            plan,
            repository=repository,
            bar_source=_Bars(),  # type: ignore[arg-type]
            chain_service=_Chain(),  # type: ignore[arg-type]
            quote_client=_QuoteClient(),
            evaluated_at=datetime(2026, 8, 4, 11, 18, 30, tzinfo=UTC),
        )
    )

    assert payload["observation_slot_ordinal"] == 1
    assert len(payload["candidates"][0]["bars"]) == 1
    assert payload["candidates"][0]["quotes"][0]["contract_type"] == "CALL"
    assert payload["candidates"][0]["quotes"][0]["quote_time"] == (
        "2026-08-04T11:18:30Z"
    )
    assert payload["candidates"][0]["diagnostics"]["comparable"] is True
    assert payload["content_hash"] == canonical_sha256(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )


def test_live_export_provider_timeout_keeps_paired_cycle_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "timeout.sqlite3"
    )

    class _Bars:
        async def warm_start(self, symbol, start, end):
            del start, end
            return [
                Bar(
                    symbol,
                    datetime(2026, 8, 4, 11, 17, tzinfo=UTC),
                    99,
                    101,
                    98,
                    100,
                    10,
                )
            ]

    class _TimedOutChain:
        async def get_chain(self, symbol, **kwargs):
            del symbol, kwargs
            raise httpx.TimeoutException("fixture chain timeout")

    class _QuoteClient:
        async def quote(self, option_symbol):
            raise AssertionError(
                f"quote must not run without selection: {option_symbol}"
            )

    payload = asyncio.run(
        export_live_cycle_input(
            plan,
            repository=repository,
            bar_source=_Bars(),  # type: ignore[arg-type]
            chain_service=_TimedOutChain(),  # type: ignore[arg-type]
            quote_client=_QuoteClient(),
            evaluated_at=datetime(2026, 8, 4, 11, 18, 30, tzinfo=UTC),
        )
    )

    candidate = payload["candidates"][0]
    assert candidate["quotes"] == []
    assert candidate["diagnostics"]["comparable"] is False
    assert candidate["diagnostics"]["errors"][0]["error_type"] == ("TimeoutException")
    receipt = run_observation_cycle(
        plan,
        payload,
        repository=repository,
        receipt_path=(
            tmp_path / "artifacts" / "chart_scenarios" / "timeout.receipt.json"
        ),
    )
    assert receipt["status"] == "succeeded"
    assert receipt["paired_fact_proof_count"] == 1
    assert (
        receipt["candidate_diagnostics"][candidate["candidate_id"]]["comparable"]
        is False
    )


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
    with pytest.raises(
        BundleValidationError,
        match="missing compatible profiles|policy/economics material",
    ):
        validate_bundle(missing)

    mismatched = _bundle_payload()
    mismatched["exit_policy_registry"][ExitProfile.TREND_CONTINUATION.value] = _policy(
        ExitProfile.RANGE_EXPANSION
    ).model_dump(mode="json")
    with pytest.raises(
        BundleValidationError,
        match="unsupported exit_family|selected policy differs|policy/economics material",
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
        BundleValidationError,
        match="unsupported risk-envelope semantics|policy/economics material",
    ):
        validate_bundle(unsupported)

    unsupported_family = _bundle_payload()
    unsupported_family["exit_policy_registry"][ExitProfile.FLASH_REVERSAL.value][
        "stop_family"
    ] = "underlying_atr"
    with pytest.raises(
        BundleValidationError,
        match="unsupported stop_family|policy/economics material",
    ):
        validate_bundle(unsupported_family)


def test_bundle_rejects_obsolete_self_signed_v1_registry_manifests() -> None:
    legacy = _bundle_payload()
    legacy["campaign_manifest"]["schema"] = "tradelab.market_context_campaign.v1"
    legacy["campaign_manifest"]["content_hash"] = "sha256:" + canonical_sha256(
        {
            key: value
            for key, value in legacy["campaign_manifest"].items()
            if key != "content_hash"
        }
    )
    legacy["campaign_manifest_hash"] = legacy["campaign_manifest"]["content_hash"]
    legacy["run_manifest"]["schema"] = "tradelab.market_context_run.v1"
    legacy["run_manifest"]["content_hash"] = "sha256:" + canonical_sha256(
        {
            key: value
            for key, value in legacy["run_manifest"].items()
            if key != "content_hash"
        }
    )
    legacy["run_manifest_hash"] = legacy["run_manifest"]["content_hash"]
    with pytest.raises(BundleValidationError, match="v2"):
        validate_bundle(legacy)


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
    drifted_plan = plan.model_copy(update={"cost_model": changed_cost})
    with pytest.raises(
        BundleValidationError,
        match="cost model hash mismatch|policy/economics material",
    ):
        BrokerInertScenarioObserver(
            ScenarioEventRepository(
                tmp_path / "artifacts" / "chart_scenarios" / "drift.sqlite3"
            ),
            plan=drifted_plan,
        )


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
        _quote("q-stalled", 1.20, "2026-08-04T15:15:00Z")
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
    canonical = evaluate_profile_exit(
        fields=ProfileExitFields.from_management_spec(policy),
        entry_premium=1.0,
        quantity=1,
        market=ProfileMarketView(current_premium=1.20, bar_time_et=time(11, 15)),
        entry_time=entry.quote_time,
        now=stalled.quote_time,
        state=ProfileExitState.new(1.0, 1),
        require_bar_time_for_eod=True,
    )
    assert canonical.rule.value == "no_progress"


def test_eod_hard_flat_precedes_staged_target_like_canonical_bhiksha() -> None:
    policy = _policy(ExitProfile.TREND_CONTINUATION).model_copy(
        update={
            "target_1_r": 1.0,
            "target_2_r": 2.0,
            "target_1_quantity": 0.6,
            "initial_stop_pct": 0.4,
        }
    )
    entry = OptionQuoteSnapshot.from_mapping(
        _quote("q-eod-entry", 2.0, "2026-08-04T18:00:00Z")
    )
    target_at_close = OptionQuoteSnapshot.from_mapping(
        _quote("q-eod-target", 2.8, "2026-08-04T20:00:00Z")
    )
    shadow = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        entry,
        target_at_close,
        entry_time=entry.quote_time,
        evaluated_at=target_at_close.quote_time,
        management_policy=policy,
        cost_model=_cost_model(contracts=10),
        quote_eligibility_policy=_quote_policy(),
    )
    canonical = evaluate_profile_exit(
        fields=ProfileExitFields.from_management_spec(policy),
        entry_premium=2.0,
        quantity=10,
        market=ProfileMarketView(current_premium=2.8, bar_time_et=time(16, 0)),
        entry_time=entry.quote_time,
        now=target_at_close.quote_time,
        state=ProfileExitState.new(2.0, 10),
        require_bar_time_for_eod=True,
    )
    assert shadow.rule == "eod_flat"
    assert shadow.status == "exit"
    assert canonical.rule.value == "eod_flat"
    assert canonical.exit_quantity == 10


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


def test_post_entry_invalidation_is_managed_only_by_priced_frozen_exit(
    tmp_path: Path,
) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "post-entry-invalidation.sqlite3"
    )
    first = _cycle_input(plan)
    first["candidates"][0]["quotes"] = [_quote("q-entry", 1.05, "2026-08-04T11:18:00Z")]
    first["content_hash"] = canonical_sha256(
        {key: value for key, value in first.items() if key != "content_hash"}
    )
    run_observation_cycle(
        plan,
        first,
        repository=repository,
        receipt_path=(
            tmp_path / "artifacts" / "chart_scenarios" / "post-entry-first.receipt.json"
        ),
    )

    second_bars = [
        *_bars(),
        {
            "timestamp": "2026-08-04T11:57:00Z",
            "open": 100.0,
            "high": 101.0,
            "low": 95.0,
            "close": 96.0,
            "volume": None,
            "completed": True,
            "bar_id": None,
        },
        {
            "timestamp": "2026-08-04T12:36:00Z",
            "open": 96.0,
            "high": 97.0,
            "low": 93.0,
            "close": 94.0,
            "volume": None,
            "completed": True,
            "bar_id": None,
        },
    ]
    second = _cycle_input(plan, slot=2)
    second["evaluated_at"] = "2026-08-04T12:36:00Z"
    second["candidates"][0]["bars"] = second_bars
    exit_quote = _quote("q-stop", 0.60, "2026-08-04T12:36:00Z")
    exit_quote["bid"] = 0.59
    exit_quote["ask"] = 0.61
    second["candidates"][0]["quotes"] = [exit_quote]
    second["content_hash"] = canonical_sha256(
        {key: value for key, value in second.items() if key != "content_hash"}
    )
    run_observation_cycle(
        plan,
        second,
        repository=repository,
        receipt_path=(
            tmp_path
            / "artifacts"
            / "chart_scenarios"
            / "post-entry-second.receipt.json"
        ),
    )

    events = repository.events()
    assert not any(event.event_type.value == "invalidated" for event in events)
    exits = [event for event in events if event.event_type.value == "synthetic_exit"]
    assert len(exits) == 2
    assert all(isinstance(event.details["primary_net_r"], float) for event in exits)
    for scenario in plan.scenarios:
        state = repository.get_state(scenario, plan.trigger_version)
        assert state is not None
        assert state["quote_attempt_count"] == 2
        assert state["quote_eligible_count"] == 2
        assert state["quote_unavailable_count"] == 0
        assert state["primary_mae_net_r"] <= state["primary_mfe_net_r"]

    third = _cycle_input(plan, slot=3)
    carry = run_observation_cycle(
        plan,
        third,
        repository=repository,
        receipt_path=(
            tmp_path / "artifacts" / "chart_scenarios" / "post-entry-third.receipt.json"
        ),
    )
    assert carry["proof_required_candidate_ids"] == []
    assert carry["paired_fact_proof_count"] == 0
    assert len(carry["terminal_carryforwards"]) == 1
    assert all(
        item["terminal_event_hash"] and item["state_hash"]
        for item in carry["terminal_carryforwards"][0]["scenario_terminal_proofs"]
    )


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
        event.event_type.value == "entry_triggered"
        and event.scenario_id == scenario.scenario_id
        for event in repository.events()
    )
    observer.observe_one(
        _plan().scenarios[1],
        bars=_bars(),
        observation_slot_ordinal=1,
    )
    second = observer.observe_one(
        scenario,
        bars=_bars(),
        quote_path=[
            _quote("q-entry", 1.05, "2026-08-04T11:20:00Z"),
            _quote("q-exit", 2.05, "2026-08-04T11:40:00Z"),
        ],
        market_observation_id="quote-recovered",
        observation_slot_ordinal=2,
    )
    assert second.error is None
    assert (
        sum(
            event.event_type.value == "entry_triggered"
            and event.scenario_id == scenario.scenario_id
            for event in repository.events()
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
            plan=_plan(),
            quote_source=NotActuallyReadOnly(),
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
    changed_payload["plan_id"] = "different-valid-plan"
    changed_plan = validate_bundle(changed_payload)
    changed_observer = BrokerInertScenarioObserver(
        repository,
        plan=changed_plan,
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
        evaluated_at="2026-08-04T11:18:00Z",
        observation_slot_ordinal=1,
    )
    skipped_pair = observer.observe_one(
        plan.scenarios[0],
        bars=_bars(),
        evaluated_at="2026-08-04T11:18:01Z",
        observation_slot_ordinal=2,
    )
    assert skipped_pair.error is not None
    assert "before every expected arm is paired" in skipped_pair.error
    changed_bars = deepcopy(_bars())
    changed_bars[-1]["close"] = 101.5
    changed_result = observer.observe_one(
        plan.scenarios[1],
        bars=changed_bars,
        market_observation_id="different-caller-label",
        evaluated_at="2026-08-04T11:18:01Z",
        observation_slot_ordinal=1,
    )
    assert changed_result.error is not None
    assert "different market facts" in changed_result.error
    paired = observer.observe_one(
        plan.scenarios[1],
        bars=_bars(),
        market_observation_id="another-ignored-label",
        evaluated_at="2026-08-04T11:18:00Z",
        observation_slot_ordinal=1,
    )
    assert paired.error is None
    proofs = repository.paired_market_fact_proofs()
    assert len(proofs) == 1
    assert proofs[0]["paired"] is True
    assert proofs[0]["observed_arm_ids"] == [
        "chart_agentic_rerank",
        "chart_deterministic",
    ]
    assert len(proofs[0]["proof_hash"]) == 64


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
        bars=[],
        quote_path=[],
        evaluated_at="2026-08-04T16:00:00Z",
        market_observation_id="renamed-crash-cycle",
        observation_slot_ordinal=2,
    )
    assert recovered.error is None
    assert recovered.terminal
    assert recovered.status == "synthetic_exit"
    recovered_state = repository.get_state(scenario, observer.trigger_version)
    assert recovered_state is not None
    latched = recovered_state["profile_states"][scenario.exit_profile.value]
    terminal_observation = latched["terminal_observation"]
    assert recovered_state["primary_net_r"] == terminal_observation["net_r"]
    assert recovered_state["primary_gross_r"] == terminal_observation["gross_r"]
    assert recovered_state["terminal_profile"] == scenario.exit_profile.value
    assert recovered_state["terminal_quote_hash"] == latched["terminal_quote_hash"]
    assert (
        recovered.events[-1].market_observation_id
        == latched["market_fact_proof"]["slot_id"]
    )


def test_observe_cycle_pairs_every_installed_arm_and_writes_zero_effect_receipt(
    tmp_path: Path,
) -> None:
    plan = _plan()
    repository = ScenarioEventRepository(
        tmp_path / "artifacts" / "chart_scenarios" / "cycle.sqlite3"
    )
    receipt_path = tmp_path / "artifacts" / "chart_scenarios" / "cycle-receipt.json"
    receipt = run_observation_cycle(
        plan,
        _cycle_input(plan),
        repository=repository,
        receipt_path=receipt_path,
    )
    assert receipt["status"] == "succeeded"
    assert receipt["scenario_count"] == len(plan.scenarios)
    assert receipt["paired_fact_proof_count"] == 1
    assert receipt["broker_effect_count"] == 0
    assert not any(receipt["effects"].values())
    proof = receipt["paired_fact_proofs"][0]
    assert proof[
        "treatment_manifest_hash"
    ] == plan.treatment_manifest_hash.removeprefix("sha256:")
    assert proof["cartographer_export_hash"] == plan.cartographer_export_hash
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt

    drifted = deepcopy(_cycle_input(plan))
    drifted["evaluated_at"] = "2026-08-04T11:18:01Z"
    drifted["content_hash"] = canonical_sha256(
        {key: value for key, value in drifted.items() if key != "content_hash"}
    )
    failed = run_observation_cycle(
        plan,
        drifted,
        repository=repository,
        receipt_path=receipt_path,
    )
    assert failed["status"] == "failed"
    assert all("different market facts" in error for error in failed["errors"])


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
            "--observation-slot",
            "1",
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
