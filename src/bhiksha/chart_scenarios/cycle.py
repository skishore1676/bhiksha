"""Run one paired, broker-inert observation cycle over an installed plan."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector

from .models import as_utc, timestamp_json
from .observer import BrokerInertScenarioObserver
from .paths import require_experiment_path
from .repository import IdempotencyConflict, ScenarioEventRepository
from .timeframes import is_xnys_session_date
from .triggers import normalize_bars
from .validation import ShadowPlan, validate_bundle


_BAR_PROVENANCE_SCHEMA = "bhiksha.chart-scenario-bar-provenance.v1"
_BAR_AGGREGATION_IMPLEMENTATION = "xnys-session-anchor-v1"

CYCLE_INPUT_SCHEMA = "bhiksha.chart-scenario-cycle-input.v2"
CYCLE_RECEIPT_SCHEMA = "bhiksha.chart-scenario-cycle-receipt.v2"


def _hash_payload(value: Mapping[str, Any], *, omit: str) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != omit})


def validate_cycle_input(
    value: Mapping[str, Any], *, plan: ShadowPlan
) -> dict[str, Any]:
    """Validate a read-only snapshot exported from Bhiksha's data seams."""

    expected = {
        "schema_version",
        "plan_hash",
        "run_manifest_hash",
        "treatment_manifest_hash",
        "observation_slot_ordinal",
        "evaluated_at",
        "candidates",
        "content_hash",
    }
    if set(value) != expected:
        raise ValueError("cycle input must declare exact top-level fields")
    if value.get("schema_version") != CYCLE_INPUT_SCHEMA:
        raise ValueError("unsupported cycle input schema")
    if str(value.get("plan_hash", "")).removeprefix("sha256:") != plan.plan_hash:
        raise ValueError("cycle input plan_hash differs from installed plan")
    if str(value.get("run_manifest_hash", "")).removeprefix(
        "sha256:"
    ) != plan.run_manifest_hash.removeprefix("sha256:"):
        raise ValueError("cycle input run_manifest_hash differs from installed plan")
    if str(value.get("treatment_manifest_hash", "")).removeprefix(
        "sha256:"
    ) != plan.treatment_manifest_hash.removeprefix("sha256:"):
        raise ValueError(
            "cycle input treatment_manifest_hash differs from installed plan"
        )
    ordinal = value.get("observation_slot_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise ValueError("cycle input observation_slot_ordinal must be positive")
    evaluated_at = timestamp_json(as_utc(value.get("evaluated_at")))
    candidates = value.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(
        candidates, (str, bytes, bytearray)
    ):
        raise TypeError("cycle input candidates must be an array")
    expected_candidates = {
        scenario.candidate_id: scenario.symbol for scenario in plan.scenarios
    }
    normalized: dict[str, dict[str, Any]] = {}
    candidate_fields = {
        "candidate_id",
        "symbol",
        "bars_by_timeframe",
        "option_selection",
        "quotes",
        "diagnostics",
    }
    for raw in candidates:
        if not isinstance(raw, Mapping) or set(raw) != candidate_fields:
            raise ValueError("cycle candidate must declare exact fields")
        candidate_id = str(raw["candidate_id"])
        symbol = str(raw["symbol"])
        if candidate_id in normalized:
            raise ValueError(f"duplicate cycle candidate: {candidate_id}")
        if expected_candidates.get(candidate_id) != symbol:
            raise ValueError("cycle candidate identity differs from installed plan")
        scenarios = [
            scenario
            for scenario in plan.scenarios
            if scenario.candidate_id == candidate_id
        ]
        bars_by_timeframe = raw["bars_by_timeframe"]
        quotes = raw["quotes"]
        option_selection = _validate_option_selection(
            raw["option_selection"],
            plan=plan,
            candidate_id=candidate_id,
            symbol=symbol,
            direction=scenarios[0].direction.value,
            evaluated_at=evaluated_at,
        )
        diagnostics = raw["diagnostics"]
        required_timeframes = {
            condition.timeframe
            for scenario in scenarios
            for condition in (
                scenario.entry_condition,
                scenario.validation_condition,
                scenario.invalidation_condition,
            )
        }
        if not isinstance(bars_by_timeframe, Mapping) or set(
            bars_by_timeframe
        ) != required_timeframes:
            raise ValueError("cycle bars_by_timeframe must exactly cover condition timeframes")
        normalized_series: dict[str, dict[str, Any]] = {}
        fact_deadline = min(
            as_utc(evaluated_at),
            min(scenario.observation_window.end_at for scenario in scenarios),
        )
        for timeframe, raw_series in bars_by_timeframe.items():
            if not isinstance(raw_series, Mapping) or set(raw_series) != {
                "timeframe",
                "provenance",
                "bars",
            } or raw_series.get("timeframe") != timeframe:
                raise ValueError("cycle timeframe series has invalid provenance")
            bars = raw_series["bars"]
            if not isinstance(bars, list) or not all(type(item) is dict for item in bars):
                raise TypeError("cycle bars must be exact raw JSON objects")
            normalized_bars = normalize_bars(bars)
            if any(bar.timestamp > fact_deadline for bar in normalized_bars):
                raise ValueError("cycle completed bar exceeds temporal cutoff")
            provenance = _validate_bar_provenance(
                timeframe, raw_series["provenance"], bars
            )
            _validate_bar_intervals(timeframe, normalized_bars)
            normalized_series[str(timeframe)] = {
                "timeframe": str(timeframe),
                "provenance": provenance,
                "bars": bars,
            }
        if not isinstance(quotes, list) or not all(
            type(item) is dict for item in quotes
        ):
            raise TypeError("cycle quotes must be exact raw JSON objects")
        selected_symbol = option_selection["effective_selected_option_symbol"]
        if quotes and not selected_symbol:
            raise ValueError("cycle quotes require a selector-proved option identity")
        for quote in quotes:
            if quote.get("option_symbol") != selected_symbol:
                raise ValueError("cycle quote differs from selector-proved option identity")
            if quote.get("is_selected") is not True:
                raise ValueError("cycle quote must declare the selector-proved contract")
        if not isinstance(diagnostics, Mapping):
            raise TypeError("cycle candidate diagnostics must be an object")
        normalized[candidate_id] = {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "bars_by_timeframe": normalized_series,
            "option_selection": option_selection,
            "quotes": quotes,
            "diagnostics": dict(diagnostics),
        }
    if set(normalized) != set(expected_candidates):
        raise ValueError(
            "cycle input must cover every installed candidate exactly once"
        )
    computed = _hash_payload(value, omit="content_hash")
    if str(value.get("content_hash", "")).removeprefix("sha256:") != computed:
        raise ValueError("cycle input content_hash mismatch")
    return {
        **dict(value),
        "evaluated_at": evaluated_at,
        "candidates": normalized,
        "content_hash": computed,
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = require_experiment_path(path, role="receipt")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _terminal_carryforward(
    candidate_id: str,
    states: Sequence[tuple[Any, Mapping[str, Any] | None]],
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "scenario_terminal_proofs": [
            {
                "scenario_id": scenario.scenario_id,
                "arm_id": scenario.arm_id.value,
                "terminal_reason": state.get("terminal_reason"),
                "terminal_event_hash": state.get("last_event_hash"),
                "state_hash": canonical_sha256(state),
            }
            for scenario, state in states
            if state is not None
        ],
    }


def run_observation_cycle(
    plan: ShadowPlan,
    cycle_input: Mapping[str, Any],
    *,
    repository: ScenarioEventRepository,
    receipt_path: str | Path,
    cycle_input_path: str | Path | None = None,
) -> dict[str, Any]:
    """Observe every installed scenario from one candidate-keyed fact snapshot."""

    sealed_plan = validate_bundle(plan.model_dump(mode="json"))
    cycle = validate_cycle_input(cycle_input, plan=sealed_plan)
    input_artifact_path = (
        require_experiment_path(cycle_input_path, role="cycle input")
        if cycle_input_path is not None
        else None
    )
    input_artifact_hash = canonical_sha256(cycle)
    existing_receipt = _existing_cycle_receipt(
        Path(receipt_path), plan=sealed_plan, cycle=cycle
    )
    if existing_receipt is not None:
        return existing_receipt
    observer = BrokerInertScenarioObserver(repository, plan=sealed_plan)
    terminal_carryforwards: list[dict[str, Any]] = []
    proof_required_candidate_ids: list[str] = []
    for candidate_id in cycle["candidates"]:
        scenarios = [
            scenario
            for scenario in sealed_plan.scenarios
            if scenario.candidate_id == candidate_id
        ]
        states = [
            (scenario, repository.get_state(scenario, sealed_plan.trigger_version))
            for scenario in scenarios
        ]
        if states and all(state and state.get("terminal") for _, state in states):
            terminal_carryforwards.append(_terminal_carryforward(candidate_id, states))
        else:
            proof_required_candidate_ids.append(candidate_id)
    results: list[dict[str, Any]] = []
    for candidate_id, facts in cycle["candidates"].items():
        selection = facts["option_selection"]
        if selection["mode"] != "persisted_contract":
            continue
        persisted = {
            str(state.get("selected_option_symbol"))
            for scenario in sealed_plan.scenarios
            if scenario.candidate_id == candidate_id
            if (state := repository.get_state(scenario, sealed_plan.trigger_version))
            and state.get("selected_option_symbol")
        }
        if persisted != {selection["effective_selected_option_symbol"]}:
            raise ValueError(
                "persisted-contract selector evidence differs from durable scenario state"
            )
    for scenario in sorted(
        sealed_plan.scenarios,
        key=lambda item: (item.candidate_id, item.arm_id.value, item.scenario_id),
    ):
        facts = cycle["candidates"][scenario.candidate_id]
        result = observer.observe_one(
            scenario,
            bars_by_timeframe=facts["bars_by_timeframe"],
            quote_path=facts["quotes"],
            option_selection=facts["option_selection"],
            evaluated_at=cycle["evaluated_at"],
            observation_slot_ordinal=cycle["observation_slot_ordinal"],
        )
        results.append(result.to_dict())
    proofs = [
        proof
        for proof in repository.paired_market_fact_proofs()
        if proof["slot_ordinal"] == cycle["observation_slot_ordinal"]
        and proof["run_id"] == sealed_plan.run_manifest["run_id"]
    ]
    errors = [item["error"] for item in results if item["error"] is not None]
    proved_candidates = {proof["candidate_id"] for proof in proofs}
    recovered_without_current_facts: set[str] = set()
    for candidate_id in set(proof_required_candidate_ids) - proved_candidates:
        scenarios = [
            scenario
            for scenario in sealed_plan.scenarios
            if scenario.candidate_id == candidate_id
        ]
        states = [
            (scenario, repository.get_state(scenario, sealed_plan.trigger_version))
            for scenario in scenarios
        ]
        if states and all(state and state.get("terminal") for _, state in states):
            recovered_without_current_facts.add(candidate_id)
            terminal_carryforwards.append(
                _terminal_carryforward(candidate_id, states)
            )
    proof_required_candidate_ids = [
        candidate_id
        for candidate_id in proof_required_candidate_ids
        if candidate_id not in recovered_without_current_facts
    ]
    missing_proofs = sorted(set(proof_required_candidate_ids) - proved_candidates)
    errors.extend(
        f"missing paired market-fact proof for active candidate {candidate_id}"
        for candidate_id in missing_proofs
    )
    body = {
        "schema_version": CYCLE_RECEIPT_SCHEMA,
        "status": "succeeded" if not errors else "failed",
        # A receipt is a pure function of the sealed cycle input.  Wall-clock
        # receipt creation time made clean replay byte-different.
        "created_at": cycle["evaluated_at"],
        "plan_hash": sealed_plan.plan_hash,
        "run_manifest_hash": sealed_plan.run_manifest_hash,
        "treatment_manifest_hash": sealed_plan.treatment_manifest_hash,
        "cycle_input_hash": cycle["content_hash"],
        "cycle_input_artifact_path": (
            str(input_artifact_path) if input_artifact_path is not None else None
        ),
        "cycle_input_artifact_hash": input_artifact_hash,
        "observation_slot_ordinal": cycle["observation_slot_ordinal"],
        "evaluated_at": cycle["evaluated_at"],
        "scenario_count": len(results),
        "paired_fact_proof_count": len(proofs),
        "paired_fact_proofs": proofs,
        "proof_required_candidate_ids": proof_required_candidate_ids,
        "terminal_carryforwards": terminal_carryforwards,
        "candidate_diagnostics": {
            candidate_id: facts["diagnostics"]
            for candidate_id, facts in cycle["candidates"].items()
        },
        "results": results,
        "errors": errors,
        "broker_effect_count": 0,
        "effects": {
            "broker": False,
            "orders": False,
            "auth": False,
            "schedule": False,
            "external_send": False,
        },
    }
    receipt = {**body, "receipt_hash": canonical_sha256(body)}
    _write_atomic(Path(receipt_path), receipt)
    return receipt


def _validate_bar_intervals(timeframe: str, bars: Sequence[Any]) -> None:
    if timeframe not in {"39m", "daily"}:
        raise ValueError(f"unsupported condition timeframe: {timeframe}")
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    seen_sessions: set[object] = set()
    prior_session: object | None = None
    prior_offset: int | None = None
    for bar in bars:
        local = bar.timestamp.astimezone(eastern)
        session_day = local.date()
        if not is_xnys_session_date(session_day):
            raise ValueError("completed bar is outside an XNYS session")
        if timeframe == "daily":
            if session_day in seen_sessions:
                raise ValueError("daily evidence contains multiple bars for one session")
            seen_sessions.add(session_day)
            continue
        session_open = local.replace(hour=9, minute=30, second=0, microsecond=0)
        offset = int((local - session_open).total_seconds() // 60)
        if offset < 0 or offset >= 390 or offset % 39 != 0:
            raise ValueError("39m bar is not anchored to the XNYS 09:30 session open")
        if prior_session == session_day and prior_offset is not None and offset <= prior_offset:
            raise ValueError("39m bars must be strictly ordered within each session")
        prior_session, prior_offset = session_day, offset


def _validate_bar_provenance(
    timeframe: str, value: Any, raw_bars: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("cycle timeframe provenance must be an object")
    expected = {
        "schema_version",
        "implementation",
        "calendar",
        "timezone",
        "session_anchor",
        "interval",
        "completed_through",
        "source_bar_count",
        "source_hash",
        "output_hash",
        "content_hash",
    }
    if set(value) != expected:
        raise ValueError("cycle timeframe provenance must declare exact fields")
    if (
        value.get("schema_version") != _BAR_PROVENANCE_SCHEMA
        or value.get("implementation") != _BAR_AGGREGATION_IMPLEMENTATION
        or value.get("calendar") != "XNYS"
        or value.get("timezone") != "America/New_York"
        or value.get("session_anchor") != "09:30"
        or value.get("interval") != timeframe
    ):
        raise ValueError("cycle timeframe aggregation provenance is unsupported")
    if value.get("output_hash") != canonical_sha256(list(raw_bars)):
        raise ValueError("cycle timeframe output hash mismatch")
    body = {key: item for key, item in value.items() if key != "content_hash"}
    if value.get("content_hash") != canonical_sha256(body):
        raise ValueError("cycle timeframe provenance content hash mismatch")
    completed_through = value.get("completed_through")
    if completed_through is not None:
        timestamp_json(as_utc(completed_through))
    count = value.get("source_bar_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("cycle timeframe source_bar_count must be non-negative")
    if not isinstance(value.get("source_hash"), str):
        raise ValueError("cycle timeframe source_hash is required")
    return dict(value)


def _validate_option_selection(
    value: Any,
    *,
    plan: ShadowPlan,
    candidate_id: str,
    symbol: str,
    direction: str,
    evaluated_at: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("cycle option_selection must be an object")
    expected = {
        "schema_version",
        "mode",
        "policy_hash",
        "evaluated_at",
        "request",
        "contracts",
        "canonical_selected_option_symbol",
        "effective_selected_option_symbol",
        "receipt_hash",
    }
    if set(value) != expected:
        raise ValueError("cycle option_selection must declare exact fields")
    if (
        value.get("schema_version")
        != "bhiksha.chart-scenario-option-selection.v1"
        or value.get("mode") not in {"canonical_selector", "persisted_contract"}
        or value.get("policy_hash") != plan.option_selection_policy.content_hash
        or timestamp_json(as_utc(value.get("evaluated_at"))) != evaluated_at
    ):
        raise ValueError("cycle option_selection identity is invalid")
    request = value.get("request")
    request_fields = {
        "deployment_id",
        "symbol",
        "direction",
        "signal_timestamp",
        "execution_profile",
        "execution_params",
    }
    if not isinstance(request, Mapping) or set(request) != request_fields:
        raise ValueError("cycle selector request must declare exact fields")
    expected_request = OptionSelectionRequest(
        deployment_id=f"chart-scenario:{candidate_id}",
        symbol=symbol,
        direction=SignalDirection(direction),
        signal_timestamp=as_utc(evaluated_at),
        execution_profile="chart_scenario_shadow_v1",
        execution_params=plan.option_selection_policy.selector_params(),
    )
    if (
        request.get("deployment_id") != expected_request.deployment_id
        or request.get("symbol") != symbol
        or request.get("direction") != direction
        or timestamp_json(as_utc(request.get("signal_timestamp"))) != evaluated_at
        or request.get("execution_profile") != expected_request.execution_profile
        or request.get("execution_params") != expected_request.execution_params
    ):
        raise ValueError("cycle selector request differs from the frozen policy")
    raw_contracts = value.get("contracts")
    if not isinstance(raw_contracts, list) or not all(
        isinstance(item, Mapping) for item in raw_contracts
    ):
        raise TypeError("cycle selector contracts must be an array")
    contract_fields = {
        "option_symbol",
        "underlying_symbol",
        "contract_type",
        "expiration_date",
        "dte",
        "strike",
        "delta",
        "bid",
        "ask",
        "open_interest",
    }
    if any(set(item) != contract_fields for item in raw_contracts):
        raise ValueError("cycle selector contract snapshot has non-exact fields")
    contracts = [OptionContractSnapshot(**dict(item)) for item in raw_contracts]
    symbols = [contract.option_symbol for contract in contracts]
    if len(symbols) != len(set(symbols)):
        raise ValueError("cycle selector contracts contain duplicate option symbols")
    signal_date = as_utc(evaluated_at).date()
    required_type = (
        plan.option_selection_policy.long_signal_contract_type
        if direction == "long"
        else plan.option_selection_policy.short_signal_contract_type
    )
    for contract in contracts:
        try:
            expiration = date.fromisoformat(contract.expiration_date)
        except ValueError as exc:
            raise ValueError("cycle selector expiration_date is invalid") from exc
        if contract.dte != (expiration - signal_date).days:
            raise ValueError("cycle selector DTE does not reproduce from expiration")
        if contract.underlying_symbol != symbol:
            raise ValueError("cycle selector contract underlying is not normalized")
        if contract.contract_type != contract.contract_type.upper():
            raise ValueError("cycle selector contract type is not normalized")
        if contract.contract_type not in {required_type, "CALL", "PUT"}:
            raise ValueError("cycle selector contract type is unsupported")
    try:
        selected = SingleLegOptionSelector().select(expected_request, contracts)
        canonical_symbol: str | None = selected.option_symbol
    except SelectorEmptyError:
        canonical_symbol = None
    if value.get("canonical_selected_option_symbol") != canonical_symbol:
        raise ValueError("cycle canonical selector result does not reproduce")
    effective = value.get("effective_selected_option_symbol")
    if value.get("mode") == "canonical_selector" and effective != canonical_symbol:
        raise ValueError("cycle effective contract differs from canonical selector")
    if effective is not None and (not isinstance(effective, str) or not effective):
        raise ValueError("cycle effective option identity is invalid")
    body = {key: item for key, item in value.items() if key != "receipt_hash"}
    if value.get("receipt_hash") != canonical_sha256(body):
        raise ValueError("cycle option selection receipt hash mismatch")
    return dict(value)


def _existing_cycle_receipt(
    path: Path, *, plan: ShadowPlan, cycle: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    expected_hash = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "receipt_hash"}
    )
    if (
        receipt.get("schema_version") != CYCLE_RECEIPT_SCHEMA
        or receipt.get("receipt_hash") != expected_hash
        or receipt.get("plan_hash") != plan.plan_hash
        or str(receipt.get("run_manifest_hash", "")).removeprefix("sha256:")
        != plan.run_manifest_hash.removeprefix("sha256:")
        or receipt.get("observation_slot_ordinal")
        != cycle["observation_slot_ordinal"]
    ):
        raise IdempotencyConflict("existing cycle receipt identity is invalid")
    if receipt.get("cycle_input_hash") != cycle["content_hash"]:
        raise IdempotencyConflict(
            "observation slot retry used a different cycle input hash"
        )
    return receipt


__all__ = [
    "CYCLE_INPUT_SCHEMA",
    "CYCLE_RECEIPT_SCHEMA",
    "run_observation_cycle",
    "validate_cycle_input",
]
