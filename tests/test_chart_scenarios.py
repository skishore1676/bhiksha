from __future__ import annotations

import ast
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from mala_bhiksha_kernel import (
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
    load_market_context_conformance_vectors,
)

from bhiksha.chart_scenarios import (
    BrokerInertScenarioObserver,
    BundleValidationError,
    CompletedBar,
    OptionQuoteSnapshot,
    ScenarioEventRepository,
    StaticOptionSnapshotSource,
    evaluate_condition,
    install_shadow_plan,
    validate_bundle,
)


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
        stop_family="option_premium",
        stop_anchor="entry_mark",
        exit_family=profile.value.lower(),
        target_model="r_multiple",
        target_r=targets[profile],
        option_stop_fallback_pct=0.40,
        hard_flat_time_et="15:55",
        eod_flat=True,
    )


def _registry() -> dict[ExitProfile, ManagementPolicySpec]:
    return {profile: _policy(profile) for profile in ExitProfile}


def _bundle_payload() -> dict:
    vectors = load_market_context_conformance_vectors()
    manifest = ComponentManifest.model_validate(vectors["component_manifest"])
    chart = ChartEvidencePacket.model_validate(vectors["chart_evidence"])
    pool = ScenarioCandidatePool.model_validate(vectors["candidate_pool"])
    selection = ArmSelection.model_validate(vectors["arm_selection"])
    scenario = ChartScenarioSpec.model_validate(vectors["chart_scenario"])
    selected_policy = _registry()[scenario.exit_profile]
    scenario = scenario.model_copy(
        update={
            "management_policy": selected_policy,
            "exit_policy_id": selected_policy.policy_id,
            "exit_policy_schema_version": selected_policy.policy_schema_version,
            "exit_policy_hash": selected_policy.policy_hash,
        }
    )
    return {
        "schema_version": "bhiksha.chart-scenario-shadow-plan.v1",
        "plan_id": "fixture-plan",
        "trigger_version": "market-context-trigger.v1",
        "authorization_mode": "shadow",
        "source_type": "chart_scenario_experiment",
        "component_manifest": manifest.model_dump(mode="json"),
        "component_manifest_hash": manifest.manifest_hash,
        "chart_evidence": [chart.model_dump(mode="json")],
        "candidate_pool": pool.model_dump(mode="json"),
        "arm_selections": [selection.model_dump(mode="json")],
        "exit_policy_registry": {
            profile.value: policy.model_dump(mode="json")
            for profile, policy in _registry().items()
        },
        "scenarios": [scenario.model_dump(mode="json")],
    }


def _scenario() -> ChartScenarioSpec:
    return validate_bundle(_bundle_payload()).scenarios[0]


def _bars() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-04T10:00:00Z",
            "open": 98.0,
            "high": 99.0,
            "low": 97.0,
            "close": 99.0,
        },
        {
            "timestamp": "2026-08-04T10:39:00Z",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
        },
        {
            "timestamp": "2026-08-04T11:18:00Z",
            "open": 101.0,
            "high": 103.0,
            "low": 100.0,
            "close": 102.0,
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
        "provenance": {"fixture": "chart-scenario"},
    }


def test_bundle_validation_joins_exact_hashes_and_install_is_atomic(tmp_path: Path) -> None:
    payload = _bundle_payload()
    output = tmp_path / "artifacts" / "chart_scenarios" / "active_shadow_plan.json"
    receipt = tmp_path / "artifacts" / "chart_scenarios" / "active_shadow_plan.receipt.json"
    live_plan = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    live_plan.parent.mkdir(parents=True)
    live_plan.write_text("live-plan-sentinel\n", encoding="utf-8")

    installed = install_shadow_plan(payload, output_path=output, receipt_path=receipt)

    assert installed["status"] == "installed"
    assert installed["broker_effect_count"] == 0
    assert installed["identities"][0]["candidate_id"] == "candidate-1"
    assert installed["identities"][0]["component_manifest_hash"] == installed["component_manifest_hash"]
    installed_bytes = output.read_bytes()
    assert json.loads(installed_bytes)["schema_version"] == "bhiksha.chart-scenario-shadow-plan.v1"
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
def test_bundle_rejects_non_shadow_source_or_unknown_trigger(field: str, value: str) -> None:
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
    with pytest.raises(BundleValidationError, match="selected policy differs"):
        validate_bundle(mismatched)

    absent_selected_material = _bundle_payload()
    absent_selected_material["scenarios"] = [
        ChartScenarioSpec.model_validate(
            load_market_context_conformance_vectors()["chart_scenario"]
        ).model_dump(mode="json")
    ]
    with pytest.raises(BundleValidationError, match="missing selected management_policy"):
        validate_bundle(absent_selected_material)


def test_bundle_refuses_live_active_plan_paths_without_reading_them(tmp_path: Path) -> None:
    live = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"secret": "sentinel"}\n', encoding="utf-8")
    with pytest.raises(BundleValidationError, match="live active-plan path"):
        install_shadow_plan(live, output_path=tmp_path / "shadow.json")
    assert live.read_text(encoding="utf-8") == '{"secret": "sentinel"}\n'


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

    cross = evaluate_condition(condition(ConditionType.CROSS_ABOVE, level=100, level_ref="chart#level"), bars, window)
    assert cross.triggered
    hold = evaluate_condition(condition(ConditionType.HOLD_ABOVE, level=100, level_ref="chart#level", bars=2), bars, window)
    assert hold.triggered
    bars_below = bars + [
        CompletedBar(datetime(2026, 8, 4, 11, 57, tzinfo=UTC), 101, 102, 98, 99),
    ]
    below = evaluate_condition(condition(ConditionType.CROSS_BELOW, level=100, level_ref="chart#level"), bars_below, window)
    assert below.triggered
    hold_below = evaluate_condition(
        condition(ConditionType.HOLD_BELOW, level=105, level_ref="chart#level", bars=2),
        bars,
        window,
    )
    assert hold_below.triggered
    reclaim = evaluate_condition(
        condition(ConditionType.RECLAIM, level=100, level_ref="chart#level", window_seconds=3600),
        bars,
        window,
    )
    assert reclaim.triggered
    reject = evaluate_condition(
        condition(ConditionType.REJECT, level=100, level_ref="chart#level", window_seconds=3600),
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


def test_observer_is_restart_safe_terminal_and_emits_primary_and_counterfactual_marks(tmp_path: Path) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(tmp_path / "events.sqlite3")
    observer = BrokerInertScenarioObserver(repository, exit_policy_registry=_registry())
    quotes = [
        _quote("q-entry", 1.05, "2026-08-04T11:20:00Z"),
        _quote("q-range", 1.85, "2026-08-04T11:40:00Z"),
        _quote("q-trend", 2.05, "2026-08-04T11:50:00Z"),
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
    exit_events = [event for event in repository.events() if event.event_type.value == "exit_observation"]
    assert {event.details["profile"] for event in exit_events} == {"TREND_CONTINUATION", "RANGE_EXPANSION"}
    assert any(event.details["counterfactual"] for event in exit_events)
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


def test_quote_unavailable_then_recovery_does_not_duplicate_trigger(tmp_path: Path) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(tmp_path / "events.sqlite3")
    observer = BrokerInertScenarioObserver(repository, exit_policy_registry=_registry())
    first = observer.observe_one(scenario, bars=_bars(), market_observation_id="missing-quote")
    assert not first.terminal
    assert "quote_unavailable" in [event.event_type.value for event in first.events]
    trigger_count = sum(event.event_type.value == "entry_triggered" for event in repository.events())
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
    assert sum(event.event_type.value == "entry_triggered" for event in repository.events()) == trigger_count
    assert any(event.event_type.value == "synthetic_entry" for event in repository.events())
    assert repository.verify_event_chain().valid


def test_repository_detects_event_chain_tampering(tmp_path: Path) -> None:
    scenario = _scenario()
    repository = ScenarioEventRepository(tmp_path / "events.sqlite3")
    BrokerInertScenarioObserver(repository, exit_policy_registry=_registry()).observe_one(
        scenario,
        bars=_bars(),
        quote_path=[_quote("q-entry", 1.05, "2026-08-04T11:20:00Z")],
        market_observation_id="chain-observation",
    )
    with sqlite3.connect(tmp_path / "events.sqlite3") as conn:
        conn.execute("UPDATE chart_scenario_events SET event_hash=? WHERE sequence=2", ("0" * 64,))
        conn.commit()
    report = repository.verify_event_chain()
    assert not report.valid
    assert report.errors


def test_read_only_quote_source_rejects_order_capability() -> None:
    class NotActuallyReadOnly:
        def get_snapshot(self, *, scenario: object, at: datetime) -> None:
            return None

        def submit_order(self, *args: object, **kwargs: object) -> None:
            return None

    with pytest.raises(TypeError, match="prohibited order capability"):
        BrokerInertScenarioObserver(
            ScenarioEventRepository(":memory:"),
            quote_source=NotActuallyReadOnly(),
            exit_policy_registry=_registry(),
        )


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
            assert not any(name == prefix or name.startswith(prefix + ".") for name in names for prefix in prohibited_prefixes), path
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in prohibited_functions, (path, node.name)


def test_cli_install_observe_and_status_are_read_only_fixture_commands(tmp_path: Path) -> None:
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
        json.dumps({"plan": _bundle_payload(), "bars": _bars(), "quotes": [_quote("q1", 1.05, "2026-08-04T11:20:00Z")] }),
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
