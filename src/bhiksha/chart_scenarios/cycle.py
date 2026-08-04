"""Run one paired, broker-inert observation cycle over an installed plan."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from .models import as_utc, timestamp_json
from .observer import BrokerInertScenarioObserver
from .paths import require_experiment_path
from .repository import ScenarioEventRepository
from .validation import ShadowPlan, validate_bundle

CYCLE_INPUT_SCHEMA = "bhiksha.chart-scenario-cycle-input.v1"
CYCLE_RECEIPT_SCHEMA = "bhiksha.chart-scenario-cycle-receipt.v1"


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
    candidate_fields = {"candidate_id", "symbol", "bars", "quotes", "diagnostics"}
    for raw in candidates:
        if not isinstance(raw, Mapping) or set(raw) != candidate_fields:
            raise ValueError("cycle candidate must declare exact fields")
        candidate_id = str(raw["candidate_id"])
        symbol = str(raw["symbol"])
        if candidate_id in normalized:
            raise ValueError(f"duplicate cycle candidate: {candidate_id}")
        if expected_candidates.get(candidate_id) != symbol:
            raise ValueError("cycle candidate identity differs from installed plan")
        bars = raw["bars"]
        quotes = raw["quotes"]
        diagnostics = raw["diagnostics"]
        if not isinstance(bars, list) or not all(type(item) is dict for item in bars):
            raise TypeError("cycle bars must be exact raw JSON objects")
        if not isinstance(quotes, list) or not all(
            type(item) is dict for item in quotes
        ):
            raise TypeError("cycle quotes must be exact raw JSON objects")
        if not isinstance(diagnostics, Mapping):
            raise TypeError("cycle candidate diagnostics must be an object")
        normalized[candidate_id] = {
            "candidate_id": candidate_id,
            "symbol": symbol,
            "bars": bars,
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


def run_observation_cycle(
    plan: ShadowPlan,
    cycle_input: Mapping[str, Any],
    *,
    repository: ScenarioEventRepository,
    receipt_path: str | Path,
) -> dict[str, Any]:
    """Observe every installed scenario from one candidate-keyed fact snapshot."""

    sealed_plan = validate_bundle(plan.model_dump(mode="json"))
    cycle = validate_cycle_input(cycle_input, plan=sealed_plan)
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
            terminal_carryforwards.append(
                {
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
            )
        else:
            proof_required_candidate_ids.append(candidate_id)
    results: list[dict[str, Any]] = []
    for scenario in sorted(
        sealed_plan.scenarios,
        key=lambda item: (item.candidate_id, item.arm_id.value, item.scenario_id),
    ):
        facts = cycle["candidates"][scenario.candidate_id]
        result = observer.observe_one(
            scenario,
            bars=facts["bars"],
            quote_path=facts["quotes"],
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
    missing_proofs = sorted(set(proof_required_candidate_ids) - proved_candidates)
    errors.extend(
        f"missing paired market-fact proof for active candidate {candidate_id}"
        for candidate_id in missing_proofs
    )
    body = {
        "schema_version": CYCLE_RECEIPT_SCHEMA,
        "status": "succeeded" if not errors else "failed",
        "created_at": timestamp_json(datetime.now(UTC)),
        "plan_hash": sealed_plan.plan_hash,
        "run_manifest_hash": sealed_plan.run_manifest_hash,
        "treatment_manifest_hash": sealed_plan.treatment_manifest_hash,
        "cycle_input_hash": cycle["content_hash"],
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


__all__ = [
    "CYCLE_INPUT_SCHEMA",
    "CYCLE_RECEIPT_SCHEMA",
    "run_observation_cycle",
    "validate_cycle_input",
]
