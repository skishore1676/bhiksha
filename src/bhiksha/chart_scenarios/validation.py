"""Kernel-backed chart-scenario bundle validation and atomic installation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mala_bhiksha_kernel import (
    ArmSelection,
    AuthorizationMode,
    ChartEvidencePacket,
    ChartScenarioSpec,
    ComponentManifest,
    ExitProfile,
    ManagementPolicySpec,
    ScenarioCandidatePool,
    SourceType,
    canonical_sha256,
    validate_shadow_only,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .paths import require_experiment_path
from .policies import CostModel, OptionSelectionPolicy, QuoteEligibilityPolicy

DEFAULT_SHADOW_PLAN_PATH = Path("artifacts/chart_scenarios/active_shadow_plan.json")
DEFAULT_SHADOW_RECEIPT_PATH = Path(
    "artifacts/chart_scenarios/active_shadow_plan.receipt.json"
)
DEFAULT_SHADOW_DB_PATH = Path("artifacts/chart_scenarios/shadow_events.sqlite3")
SHADOW_PLAN_SCHEMA_VERSION = "bhiksha.chart-scenario-shadow-plan.v1"
INSTALL_RECEIPT_SCHEMA_VERSION = "bhiksha.chart-scenario-install-receipt.v2"
TRIGGER_VERSION = "market-context-trigger.v1"


class BundleValidationError(ValueError):
    """A packet cannot enter the experiment shadow lane."""


class InstallError(BundleValidationError):
    """Installation failed; the existing shadow artifact remains untouched."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _hash_key(raw: Mapping[str, Any], aliases: tuple[str, ...], path: str) -> str:
    for key in aliases:
        if key in raw and raw[key] not in (None, ""):
            return key
    raise BundleValidationError(f"{path} is missing its canonical content hash")


def _require_declared_hash(raw: Any, aliases: tuple[str, ...], path: str) -> None:
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        raise BundleValidationError(f"{path} must be an object")
    _hash_key(raw, aliases, path)


def _normalize_raw_bundle(data: Any) -> Any:
    if isinstance(data, ShadowPlan):
        return data
    if not isinstance(data, Mapping):
        return data
    payload = dict(data)
    if "scenarios" not in payload and "chart_scenarios" in payload:
        payload["scenarios"] = payload.pop("chart_scenarios")
    if "arm_selections" not in payload and "selections" in payload:
        payload["arm_selections"] = payload.pop("selections")
    return payload


class ShadowPlan(BaseModel):
    """The only plan shape accepted by the Bhiksha chart-scenario lane."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=False)

    schema_version: Literal[SHADOW_PLAN_SCHEMA_VERSION]
    plan_id: str = Field(min_length=1)
    trigger_version: Literal[TRIGGER_VERSION]
    authorization_mode: Literal["shadow"]
    source_type: Literal["chart_scenario_experiment"]
    campaign_manifest: dict[str, Any]
    campaign_manifest_hash: str
    run_manifest: dict[str, Any]
    run_manifest_hash: str
    treatment_manifest: dict[str, Any]
    treatment_manifest_hash: str
    cartographer_receipt: dict[str, Any]
    cartographer_receipt_hash: str
    cartographer_export_manifest: dict[str, Any]
    cartographer_export_hash: str
    target_session_date: str
    target_session_window_hash: str
    arm_b_selector_receipt: dict[str, Any]
    arm_b_selector_receipt_hash: str
    component_manifest: ComponentManifest
    component_manifest_hash: str = Field(min_length=64, max_length=64)
    chart_evidence: list[ChartEvidencePacket] = Field(min_length=1)
    candidate_pool: ScenarioCandidatePool
    arm_selections: list[ArmSelection] = Field(min_length=1)
    exit_policy_registry: dict[ExitProfile, ManagementPolicySpec] = Field(min_length=1)
    cost_model: CostModel
    quote_eligibility_policy: QuoteEligibilityPolicy
    option_selection_policy: OptionSelectionPolicy
    scenarios: list[ChartScenarioSpec] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def require_bundle_hashes(cls, data: Any) -> Any:
        payload = _normalize_raw_bundle(data)
        if isinstance(payload, ShadowPlan) or not isinstance(payload, Mapping):
            return payload
        required = (
            "schema_version",
            "plan_id",
            "trigger_version",
            "authorization_mode",
            "source_type",
            "campaign_manifest",
            "campaign_manifest_hash",
            "run_manifest",
            "run_manifest_hash",
            "treatment_manifest",
            "treatment_manifest_hash",
            "cartographer_receipt",
            "cartographer_receipt_hash",
            "cartographer_export_manifest",
            "cartographer_export_hash",
            "target_session_date",
            "target_session_window_hash",
            "arm_b_selector_receipt",
            "arm_b_selector_receipt_hash",
            "component_manifest",
            "component_manifest_hash",
            "chart_evidence",
            "candidate_pool",
            "arm_selections",
            "exit_policy_registry",
            "cost_model",
            "quote_eligibility_policy",
            "option_selection_policy",
            "scenarios",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise BundleValidationError(
                "bundle is missing required fields: " + ", ".join(missing)
            )
        _require_declared_hash(
            payload["component_manifest"],
            ("content_hash", "manifest_hash", "component_manifest_hash"),
            "component_manifest",
        )
        for index, item in enumerate(payload["chart_evidence"]):
            _require_declared_hash(
                item,
                ("content_hash", "evidence_hash", "chart_evidence_hash"),
                f"chart_evidence[{index}]",
            )
        pool = payload["candidate_pool"]
        _require_declared_hash(
            pool,
            ("content_hash", "pool_hash", "candidate_pool_hash"),
            "candidate_pool",
        )
        if isinstance(pool, Mapping):
            for index, candidate in enumerate(pool.get("candidates", [])):
                _require_declared_hash(
                    candidate,
                    ("content_hash", "candidate_hash"),
                    f"candidate_pool.candidates[{index}]",
                )
        for index, item in enumerate(payload["arm_selections"]):
            _require_declared_hash(
                item,
                ("content_hash", "selection_hash", "arm_selection_hash"),
                f"arm_selections[{index}]",
            )
        registry = payload["exit_policy_registry"]
        if not isinstance(registry, Mapping):
            raise BundleValidationError("exit_policy_registry must be an object")
        for profile, policy in registry.items():
            expected_policy_fields = set(ManagementPolicySpec.model_fields) - {
                "protective_floor_mode"
            }
            if (
                isinstance(policy, Mapping)
                and policy.get("policy_schema_version") == "exit-policy.v2"
            ):
                expected_policy_fields.add("protective_floor_mode")
            if not isinstance(policy, Mapping) or set(policy) != expected_policy_fields:
                missing = (
                    expected_policy_fields - set(policy)
                    if isinstance(policy, Mapping)
                    else expected_policy_fields
                )
                extra = (
                    set(policy) - expected_policy_fields
                    if isinstance(policy, Mapping)
                    else set()
                )
                raise BundleValidationError(
                    f"exit_policy_registry[{profile}] must explicitly declare every policy field; "
                    f"missing={sorted(missing)}, extra={sorted(extra)}"
                )
        _require_declared_hash(payload["cost_model"], ("content_hash",), "cost_model")
        _require_declared_hash(
            payload["quote_eligibility_policy"],
            ("content_hash",),
            "quote_eligibility_policy",
        )
        _require_declared_hash(
            payload["option_selection_policy"],
            ("content_hash",),
            "option_selection_policy",
        )
        for index, item in enumerate(payload["scenarios"]):
            _require_declared_hash(
                item,
                ("content_hash", "scenario_hash", "chart_scenario_hash"),
                f"scenarios[{index}]",
            )
        return payload

    @model_validator(mode="after")
    def validate_plan(self) -> ShadowPlan:
        validate_shadow_only(
            type(
                "PlanAuthorization",
                (),
                {
                    "authorization_mode": AuthorizationMode.SHADOW,
                    "source_type": SourceType.CHART_SCENARIO_EXPERIMENT,
                },
            )()
        )
        if self.component_manifest_hash != self.component_manifest.manifest_hash:
            raise BundleValidationError(
                "component_manifest_hash does not match component_manifest"
            )
        campaign_fields = {
            "schema",
            "program_id",
            "experiment_family_id",
            "experiment_version",
            "campaign_id",
            "created_at",
            "starts_on",
            "ends_on",
            "authorization_mode",
            "expected_arms",
            "treatment_manifest",
            "treatment_manifest_hash",
            "universe_hash",
            "status",
            "content_hash",
        }
        run_fields = {
            "schema",
            "program_id",
            "experiment_family_id",
            "experiment_version",
            "campaign_id",
            "run_id",
            "trading_date",
            "target_session_date",
            "as_of",
            "authorization_mode",
            "expected_arms",
            "input_hashes",
            "treatment_manifest_hash",
            "cartographer_receipt_hash",
            "cartographer_export_hash",
            "observation_window_hash",
            "status",
            "content_hash",
        }
        if (
            set(self.campaign_manifest) != campaign_fields
            or set(self.run_manifest) != run_fields
        ):
            raise BundleValidationError(
                "campaign and run manifests must be exact registered v2 artifacts"
            )
        campaign_hash = "sha256:" + canonical_sha256(
            {
                key: value
                for key, value in self.campaign_manifest.items()
                if key != "content_hash"
            }
        )
        run_hash = "sha256:" + canonical_sha256(
            {
                key: value
                for key, value in self.run_manifest.items()
                if key != "content_hash"
            }
        )
        if (
            self.campaign_manifest.get("content_hash") != campaign_hash
            or self.campaign_manifest_hash != campaign_hash
        ):
            raise BundleValidationError("campaign manifest hash mismatch")
        if (
            self.run_manifest.get("content_hash") != run_hash
            or self.run_manifest_hash != run_hash
        ):
            raise BundleValidationError("run manifest hash mismatch")
        expected_arms = ["chart_deterministic", "chart_agentic_rerank"]
        if (
            self.campaign_manifest.get("schema")
            != "tradelab.market_context_campaign.v2"
            or self.run_manifest.get("schema") != "tradelab.market_context_run.v2"
        ):
            raise BundleValidationError("shadow plan requires campaign.v2 and run.v2")
        for manifest, status in (
            (self.campaign_manifest, "authorized"),
            (self.run_manifest, "created"),
        ):
            if (
                manifest.get("authorization_mode") != "shadow"
                or manifest.get("status") != status
            ):
                raise BundleValidationError(
                    "campaign/run manifest is not shadow-authorized"
                )
            if manifest.get("expected_arms") != expected_arms:
                raise BundleValidationError(
                    "campaign/run manifest does not authorize both arms"
                )
        identity = {
            "program_id": self.candidate_pool.program_id,
            "experiment_family_id": self.candidate_pool.experiment_family_id,
            "experiment_version": self.candidate_pool.experiment_version,
            "campaign_id": self.candidate_pool.campaign_id,
        }
        if any(
            self.campaign_manifest.get(key) != value
            or self.run_manifest.get(key) != value
            for key, value in identity.items()
        ):
            raise BundleValidationError(
                "campaign/run identity does not match candidate pool"
            )
        if self.run_manifest.get("run_id") != self.candidate_pool.run_id:
            raise BundleValidationError(
                "run manifest does not authorize candidate pool run_id"
            )
        as_of = self.candidate_pool.as_of.isoformat().replace("+00:00", "Z")
        if self.run_manifest.get("as_of") != as_of:
            raise BundleValidationError(
                "run manifest as_of does not match candidate pool"
            )
        if (
            not self.campaign_manifest.get("starts_on")
            <= self.run_manifest.get("trading_date")
            <= self.campaign_manifest.get("ends_on")
        ):
            raise BundleValidationError("run date is outside campaign authorization")
        treatment_body = {
            key: value
            for key, value in self.treatment_manifest.items()
            if key != "content_hash"
        }
        treatment_hash = "sha256:" + canonical_sha256(treatment_body)
        if (
            self.treatment_manifest.get("schema")
            != "tradelab.market_context_treatment.v1"
            or self.treatment_manifest.get("content_hash") != treatment_hash
            or self.treatment_manifest_hash != treatment_hash
            or self.campaign_manifest.get("treatment_manifest")
            != self.treatment_manifest
            or self.campaign_manifest.get("treatment_manifest_hash") != treatment_hash
            or self.run_manifest.get("treatment_manifest_hash") != treatment_hash
        ):
            raise BundleValidationError("campaign/run treatment manifest mismatch")
        frozen_behavior = self.treatment_manifest.get("frozen_behavior")
        required_frozen = {
            "exit_policy_registry",
            "cost_model",
            "quote_eligibility_policy",
            "option_selection_policy",
            "selected_profile_by_thesis",
        }
        if (
            not isinstance(frozen_behavior, Mapping)
            or set(frozen_behavior) != required_frozen
        ):
            raise BundleValidationError(
                "treatment must freeze five exact policy/economics mappings"
            )
        frozen_material: dict[str, Mapping[str, Any]] = {}
        for name in sorted(required_frozen):
            item = frozen_behavior[name]
            if not isinstance(item, Mapping) or set(item) != {
                "material",
                "content_hash",
            }:
                raise BundleValidationError(
                    f"treatment frozen_behavior.{name} is invalid"
                )
            material = item.get("material")
            if not isinstance(material, Mapping):
                raise BundleValidationError(
                    f"treatment frozen_behavior.{name}.material must be an object"
                )
            material_hash = "sha256:" + canonical_sha256(material)
            if item.get("content_hash") != material_hash:
                raise BundleValidationError(
                    f"treatment frozen_behavior.{name} hash mismatch"
                )
            frozen_material[name] = material
        registry_material = {
            profile.value: policy.model_dump(mode="json")
            for profile, policy in sorted(
                self.exit_policy_registry.items(), key=lambda item: item[0].value
            )
        }
        if (
            dict(frozen_material["exit_policy_registry"]) != registry_material
            or dict(frozen_material["cost_model"])
            != self.cost_model.model_dump(mode="json")
            or dict(frozen_material["quote_eligibility_policy"])
            != self.quote_eligibility_policy.model_dump(mode="json")
            or dict(frozen_material["option_selection_policy"])
            != self.option_selection_policy.model_dump(mode="json")
        ):
            raise BundleValidationError(
                "plan policy/economics material differs from frozen treatment"
            )
        selected_profile_map = frozen_material["selected_profile_by_thesis"]
        if any(
            selected_profile_map.get(scenario.thesis_class.value)
            != scenario.exit_profile.value
            for scenario in self.scenarios
        ):
            raise BundleValidationError(
                "scenario exit profile differs from frozen treatment mapping"
            )
        if (
            str(self.campaign_manifest.get("universe_hash", "")).removeprefix("sha256:")
            != self.candidate_pool.universe_manifest_hash
        ):
            raise BundleValidationError("campaign universe differs from candidate pool")

        export_body = {
            key: value
            for key, value in self.cartographer_export_manifest.items()
            if key not in {"export_id", "export_hash"}
        }
        export_hash = canonical_sha256(export_body)
        target_window = self.cartographer_export_manifest.get("target_session_window")
        if (
            self.cartographer_export_manifest.get("schema")
            != "market_cartographer.market_context_export.v2"
            or self.cartographer_export_manifest.get("export_hash") != export_hash
            or self.cartographer_export_manifest.get("export_id")
            != f"export:{export_hash[:16]}"
            or self.cartographer_export_hash.removeprefix("sha256:") != export_hash
            or not isinstance(target_window, Mapping)
        ):
            raise BundleValidationError("Cartographer export manifest mismatch")
        window_hash = canonical_sha256(target_window)
        receipt_body = {
            key: value
            for key, value in self.cartographer_receipt.items()
            if key != "receipt_hash"
        }
        receipt_hash = canonical_sha256(receipt_body)
        if (
            self.cartographer_receipt.get("schema")
            != "market_cartographer.market_context_receipt.v2"
            or self.cartographer_receipt.get("status") != "succeeded"
            or self.cartographer_receipt.get("receipt_hash") != receipt_hash
            or self.cartographer_receipt_hash.removeprefix("sha256:") != receipt_hash
            or self.cartographer_receipt.get("run_id") != self.candidate_pool.run_id
            or self.cartographer_receipt.get("export_hash") != export_hash
            or str(
                self.cartographer_receipt.get("candidate_pool_hash", "")
            ).removeprefix("sha256:")
            != self.candidate_pool.pool_hash
            or self.cartographer_receipt.get("target_session_window") != target_window
            or str(
                self.cartographer_receipt.get("target_session_window_hash", "")
            ).removeprefix("sha256:")
            != window_hash
        ):
            raise BundleValidationError("Cartographer receipt mismatch")
        if (
            self.target_session_date
            != self.cartographer_export_manifest.get("target_session_date")
            or self.run_manifest.get("target_session_date") != self.target_session_date
            or self.run_manifest.get("trading_date") != self.target_session_date
            or self.target_session_window_hash.removeprefix("sha256:") != window_hash
            or str(self.run_manifest.get("observation_window_hash", "")).removeprefix(
                "sha256:"
            )
            != window_hash
            or str(self.run_manifest.get("cartographer_receipt_hash", "")).removeprefix(
                "sha256:"
            )
            != receipt_hash
            or str(self.run_manifest.get("cartographer_export_hash", "")).removeprefix(
                "sha256:"
            )
            != export_hash
            or any(
                candidate.observation_window.model_dump(mode="json")
                != dict(target_window)
                for candidate in self.candidate_pool.candidates
            )
        ):
            raise BundleValidationError("registered run window/input identity mismatch")

        evidence_by_id = {item.evidence_id: item for item in self.chart_evidence}
        if len(evidence_by_id) != len(self.chart_evidence):
            raise BundleValidationError("chart evidence IDs must be unique")
        for packet in self.chart_evidence:
            self._check_identity(packet, "chart evidence")

        self._check_identity(self.candidate_pool, "candidate pool")
        run_identity = (
            self.candidate_pool.program_id,
            self.candidate_pool.experiment_family_id,
            self.candidate_pool.experiment_version,
            self.candidate_pool.campaign_id,
            self.candidate_pool.run_id,
        )
        for reference in self.candidate_pool.chart_evidence_refs:
            evidence = evidence_by_id.get(reference.evidence_id)
            if (
                evidence is None
                or evidence.chart_evidence_hash != reference.evidence_hash
            ):
                raise BundleValidationError(
                    f"candidate pool evidence hash mismatch for {reference.evidence_id}"
                )
        for packet in self.chart_evidence:
            if (
                packet.program_id,
                packet.experiment_family_id,
                packet.experiment_version,
                packet.campaign_id,
                packet.run_id,
            ) != run_identity:
                raise BundleValidationError(
                    "chart evidence identity does not match candidate pool"
                )
            if packet.as_of > self.candidate_pool.as_of:
                raise BundleValidationError(
                    "chart evidence as_of is later than candidate pool as_of"
                )

        selections_by_arm: dict[str, ArmSelection] = {}
        for selection in self.arm_selections:
            self._check_identity(selection, "arm selection")
            if (
                selection.program_id,
                selection.experiment_family_id,
                selection.experiment_version,
                selection.campaign_id,
                selection.run_id,
            ) != run_identity:
                raise BundleValidationError(
                    "arm selection identity does not match candidate pool"
                )
            arm_key = selection.arm_id.value
            if arm_key in selections_by_arm:
                raise BundleValidationError(f"duplicate arm selection for {arm_key}")
            selection.validate_against_pool(self.candidate_pool)
            selections_by_arm[arm_key] = selection
        expected_arms = {"chart_deterministic", "chart_agentic_rerank"}
        if set(selections_by_arm) != expected_arms:
            raise BundleValidationError(
                "shadow plan requires both experiment arms; missing="
                + ", ".join(sorted(expected_arms - set(selections_by_arm)))
            )
        selector_receipt = dict(self.arm_b_selector_receipt)
        declared_selector_hash = str(
            selector_receipt.pop("content_hash", "")
        ).removeprefix("sha256:")
        computed_selector_hash = canonical_sha256(selector_receipt)
        selector_fields = {
            "schema",
            "status",
            "execution_mode",
            "campaign_id",
            "run_id",
            "candidate_pool_hash",
            "selection_hash",
            "provider_id",
            "model_id",
            "prompt_contract_sha256",
            "maximum",
            "agent_broker_receipt",
            "agent_broker_receipt_hash",
            "run_comparable",
            "non_comparable_reason",
        }
        frozen_ranker = self.treatment_manifest.get("ranker")
        arm_b = selections_by_arm["chart_agentic_rerank"]
        if (
            set(selector_receipt) != selector_fields
            or selector_receipt.get("schema")
            != "tradelab.market_context_ranker_receipt.v1"
            or declared_selector_hash != computed_selector_hash
            or self.arm_b_selector_receipt_hash.removeprefix("sha256:")
            != computed_selector_hash
            or not isinstance(frozen_ranker, Mapping)
            or selector_receipt.get("campaign_id") != self.candidate_pool.campaign_id
            or selector_receipt.get("run_id") != self.candidate_pool.run_id
            or str(selector_receipt.get("candidate_pool_hash", "")).removeprefix(
                "sha256:"
            )
            != self.candidate_pool.pool_hash
            or str(selector_receipt.get("selection_hash", "")).removeprefix("sha256:")
            != arm_b.selection_hash
            or any(
                selector_receipt.get(field) != frozen_ranker.get(field)
                for field in (
                    "provider_id",
                    "model_id",
                    "prompt_contract_sha256",
                    "maximum",
                )
            )
        ):
            raise BundleValidationError("Arm B selector receipt identity/hash mismatch")
        execution_mode = selector_receipt.get("execution_mode")
        broker_receipt = selector_receipt.get("agent_broker_receipt")
        broker_receipt_hash = selector_receipt.get("agent_broker_receipt_hash")
        if execution_mode == "agent_broker":
            if (
                selector_receipt.get("status") != "succeeded"
                or selector_receipt.get("run_comparable") is not True
                or selector_receipt.get("non_comparable_reason") is not None
                or not isinstance(broker_receipt, Mapping)
                or str(broker_receipt_hash).removeprefix("sha256:")
                != canonical_sha256(broker_receipt)
            ):
                raise BundleValidationError("invalid comparable Arm B selector receipt")
        elif execution_mode == "deterministic_plumbing_canary":
            if (
                selector_receipt.get("run_comparable") is not False
                or not selector_receipt.get("non_comparable_reason")
                or broker_receipt is not None
                or broker_receipt_hash is not None
            ):
                raise BundleValidationError(
                    "invalid deterministic Arm B canary receipt"
                )
        elif execution_mode == "agent_broker_failure":
            if (
                selector_receipt.get("status") != "failed"
                or selector_receipt.get("run_comparable") is not False
                or not selector_receipt.get("non_comparable_reason")
                or not isinstance(broker_receipt, Mapping)
                or str(broker_receipt_hash).removeprefix("sha256:")
                != canonical_sha256(broker_receipt)
                or arm_b.selections
                or {item.candidate_id for item in arm_b.abstentions}
                != {item.candidate_id for item in self.candidate_pool.candidates}
            ):
                raise BundleValidationError("invalid failed Arm B selector receipt")
        else:
            raise BundleValidationError("unsupported Arm B selector execution mode")

        scenario_keys: set[tuple[str, str, str, str, str]] = set()
        required_profiles = {
            profile
            for scenario in self.scenarios
            for profile in scenario.compatible_exit_profiles
        }
        missing_profiles = required_profiles - set(self.exit_policy_registry)
        if missing_profiles:
            raise BundleValidationError(
                "exit_policy_registry is missing compatible profiles: "
                + ", ".join(sorted(profile.value for profile in missing_profiles))
            )
        for profile, policy in self.exit_policy_registry.items():
            try:
                policy.policy_identity()
            except ValueError as exc:
                raise BundleValidationError(
                    f"exit_policy_registry[{profile.value}] is not canonically resolved: {exc}"
                ) from exc
            if policy.risk_envelope_enabled or policy.protective_floor_mode is not None:
                raise BundleValidationError(
                    f"exit_policy_registry[{profile.value}] uses unsupported risk-envelope semantics"
                )
            implemented = {
                "policy_schema_version": "exit-policy.v1",
                "stop_family": "premium_pct",
                "stop_anchor": "filled_option_premium",
                "target_model": "staged_r",
                "target_order_mode": "virtual_or_broker",
                "exit_family": profile.value.lower(),
            }
            for field, expected in implemented.items():
                if getattr(policy, field) != expected:
                    raise BundleValidationError(
                        f"exit_policy_registry[{profile.value}] uses unsupported {field}; "
                        f"expected {expected!r}"
                    )
            if (
                policy.initial_stop_pct is not None
                and policy.premium_disaster_stop_pct is not None
                and policy.premium_disaster_stop_pct < policy.initial_stop_pct
            ):
                raise BundleValidationError(
                    f"exit_policy_registry[{profile.value}] disaster stop is tighter than initial stop"
                )
            if policy.no_progress_seconds is not None:
                floor = policy.parameters.get("no_progress_favorable_floor_r")
                if isinstance(floor, bool) or not isinstance(floor, (int, float)):
                    raise BundleValidationError(
                        f"exit_policy_registry[{profile.value}] requires explicit "
                        "parameters.no_progress_favorable_floor_r"
                    )
        for scenario in self.scenarios:
            self._check_identity(scenario, "scenario")
            if (
                scenario.program_id,
                scenario.experiment_family_id,
                scenario.experiment_version,
                scenario.campaign_id,
                scenario.run_id,
            ) != run_identity:
                raise BundleValidationError(
                    "scenario identity does not match candidate pool"
                )
            if scenario.observation_window.start_at < self.candidate_pool.as_of:
                raise BundleValidationError(
                    "scenario observation window starts before candidate pool as_of"
                )
            if scenario.component_manifest_hash != self.component_manifest_hash:
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} has a different component manifest hash"
                )
            if scenario.cost_model_hash != self.cost_model.content_hash:
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} cost model hash mismatch"
                )
            if (
                scenario.quote_eligibility_policy_hash
                != self.quote_eligibility_policy.content_hash
            ):
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} quote policy hash mismatch"
                )
            selection = selections_by_arm.get(scenario.arm_id.value)
            if selection is None:
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} has no matching arm selection"
                )
            candidate = self.candidate_pool.candidate_for(scenario.candidate_id)
            if candidate is None:
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} references an unknown candidate"
                )
            scenario.validate_against_candidate(candidate)
            scenario.validate_against_selection(selection, self.candidate_pool)
            selected_policy = self.exit_policy_registry[scenario.exit_profile]
            selected_identity = selected_policy.policy_identity()
            if scenario.management_policy is None:
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} is missing selected management_policy"
                )
            if (
                scenario.management_policy.canonical_policy_json
                != selected_policy.canonical_policy_json
            ):
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} selected policy differs from exit_policy_registry"
                )
            if (
                scenario.exit_policy_id != selected_identity["policy_id"]
                or scenario.exit_policy_schema_version
                != selected_identity["policy_schema_version"]
                or scenario.exit_policy_hash != selected_identity["policy_hash"]
            ):
                raise BundleValidationError(
                    f"scenario {scenario.scenario_id} selected policy identity mismatch"
                )
            key = (
                scenario.campaign_id,
                scenario.run_id,
                scenario.arm_id.value,
                scenario.scenario_id,
                self.trigger_version,
            )
            if key in scenario_keys:
                raise BundleValidationError(f"duplicate scenario identity: {key}")
            scenario_keys.add(key)
            for reference in scenario.chart_evidence_refs:
                evidence = evidence_by_id.get(reference.evidence_id)
                if (
                    evidence is None
                    or evidence.chart_evidence_hash != reference.evidence_hash
                ):
                    raise BundleValidationError(
                        f"scenario {scenario.scenario_id} evidence hash mismatch for {reference.evidence_id}"
                    )
        return self

    @staticmethod
    def _check_identity(packet: Any, label: str) -> None:
        expected = {
            "program_id": "market-context-learning",
            "experiment_family_id": "morning-market-scenario-selection-shadow",
            "experiment_version": 1,
        }
        for field, value in expected.items():
            if getattr(packet, field, None) != value:
                raise BundleValidationError(f"{label} has invalid {field}")

    @property
    def plan_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def validate_bundle(payload: Mapping[str, Any] | ShadowPlan) -> ShadowPlan:
    """Validate every shared-kernel artifact and all cross-artifact joins."""

    try:
        return ShadowPlan.model_validate(payload)
    except BundleValidationError:
        raise
    except Exception as exc:
        raise BundleValidationError(str(exc)) from exc


class ChartScenarioBundleValidator:
    """Object-shaped adapter for integrations that prefer a validator instance."""

    def validate(self, payload: Mapping[str, Any] | ShadowPlan) -> ShadowPlan:
        return validate_bundle(payload)

    def load(self, path: str | Path) -> ShadowPlan:
        return load_bundle(path)


def load_bundle(path: str | Path) -> ShadowPlan:
    source = Path(path)
    _guard_experiment_path(source, role="input")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BundleValidationError(
            f"cannot read chart-scenario bundle: {exc}"
        ) from exc
    return validate_bundle(payload)


def _guard_experiment_path(path: Path, *, role: str) -> None:
    if role in {"output", "receipt", "database"}:
        try:
            require_experiment_path(path, role=role)
        except ValueError as exc:
            raise BundleValidationError(str(exc)) from exc
        return
    resolved = path.expanduser().resolve()
    if resolved.name == "active_plan.json" or "playbook" in resolved.parts:
        raise BundleValidationError(
            f"chart-scenario lane refuses to {role} the live active-plan path: {resolved}"
        )


def _default_receipt_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.receipt.json")


def _write_atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _input_bytes(
    source: str | Path | Mapping[str, Any],
) -> tuple[bytes, Mapping[str, Any]]:
    if isinstance(source, Mapping):
        encoded = (
            json.dumps(
                source, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8")
        return encoded, source
    source_path = Path(source)
    _guard_experiment_path(source_path, role="input")
    encoded = source_path.read_bytes()
    return encoded, json.loads(encoded.decode("utf-8"))


def _failure_receipt(
    *,
    output: Path,
    receipt: Path,
    error: Exception,
    input_sha256: str | None,
) -> dict[str, Any]:
    body = {
        "receipt_schema_version": INSTALL_RECEIPT_SCHEMA_VERSION,
        "receipt_id": "install-failure-" + (input_sha256 or "unknown")[:16],
        "status": "failed",
        "created_at": _utc_now(),
        "artifact_path": str(output),
        "receipt_path": str(receipt),
        "input_sha256": input_sha256,
        "error_type": type(error).__name__,
        "error": str(error),
        "broker_effect_count": 0,
    }
    return {**body, "receipt_hash": canonical_sha256(body)}


def install_shadow_plan(
    source: str | Path | Mapping[str, Any],
    *,
    output_path: str | Path = DEFAULT_SHADOW_PLAN_PATH,
    receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate then atomically replace the experiment-only shadow plan."""

    output = Path(output_path)
    receipt = (
        Path(receipt_path)
        if receipt_path is not None
        else _default_receipt_path(output)
    )
    _guard_experiment_path(output, role="output")
    _guard_experiment_path(receipt, role="receipt")
    if output.expanduser().resolve() == receipt.expanduser().resolve():
        raise BundleValidationError("shadow plan and install receipt paths must differ")
    _guard_experiment_path(output, role="write")
    _guard_experiment_path(receipt, role="write")
    input_digest: str | None = None
    try:
        encoded, payload = _input_bytes(source)
        input_digest = hashlib.sha256(encoded).hexdigest()
        plan = validate_bundle(payload)
        _write_atomic_json(output, plan.to_payload())
        receipt_body = {
            "receipt_schema_version": INSTALL_RECEIPT_SCHEMA_VERSION,
            "receipt_id": "install-" + plan.plan_hash[:32],
            "status": "installed",
            "created_at": _utc_now(),
            "artifact_path": str(output),
            "receipt_path": str(receipt),
            "input_sha256": input_digest,
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "run_manifest_hash": plan.run_manifest_hash,
            "treatment_manifest_hash": plan.treatment_manifest_hash,
            "cartographer_receipt_hash": plan.cartographer_receipt_hash,
            "cartographer_export_hash": plan.cartographer_export_hash,
            "option_selection_policy_hash": plan.option_selection_policy.content_hash,
            "arm_b_selector_receipt_hash": plan.arm_b_selector_receipt_hash,
            "target_session_date": plan.target_session_date,
            "target_session_window_hash": plan.target_session_window_hash,
            "scenario_count": len(plan.scenarios),
            "scenario_hashes": [scenario.scenario_hash for scenario in plan.scenarios],
            "identities": [
                {
                    "program_id": scenario.program_id,
                    "experiment_family_id": scenario.experiment_family_id,
                    "experiment_version": scenario.experiment_version,
                    "campaign_id": scenario.campaign_id,
                    "run_id": scenario.run_id,
                    "arm_id": scenario.arm_id.value,
                    "scenario_id": scenario.scenario_id,
                    "candidate_id": scenario.candidate_id,
                    "symbol": scenario.symbol,
                    "direction": scenario.direction.value,
                    "thesis_class": scenario.thesis_class.value,
                    "scenario_hash": scenario.scenario_hash,
                    "candidate_pool_hash": scenario.candidate_pool_hash,
                    "selection_packet_hash": scenario.selection_packet_hash,
                    "component_manifest_hash": scenario.component_manifest_hash,
                    "chart_evidence_hashes": [
                        item.evidence_hash for item in scenario.chart_evidence_refs
                    ],
                    "exit_policy_hash": scenario.exit_policy_hash,
                }
                for scenario in plan.scenarios
            ],
            "component_manifest_hash": plan.component_manifest_hash,
            "candidate_pool_hash": plan.candidate_pool.pool_hash,
            "broker_effect_count": 0,
        }
        receipt_payload = {
            **receipt_body,
            "receipt_hash": canonical_sha256(receipt_body),
        }
        _write_atomic_json(receipt, receipt_payload)
        return receipt_payload
    except Exception as exc:
        failure = _failure_receipt(
            output=output,
            receipt=receipt,
            error=exc,
            input_sha256=input_digest,
        )
        try:
            _write_atomic_json(receipt, failure)
        except Exception:  # noqa: BLE001,S110 - preserve the original install failure
            # The original validation/write error is the useful failure.  The
            # caller still sees it if filesystem metadata is read-only.
            pass
        if isinstance(exc, BundleValidationError):
            raise
        raise InstallError(str(exc)) from exc


def read_installed_plan(path: str | Path = DEFAULT_SHADOW_PLAN_PATH) -> ShadowPlan:
    """Read and revalidate only the experiment shadow artifact."""

    return load_bundle(path)


class AtomicShadowPlanInstaller:
    """Object-shaped adapter around :func:`install_shadow_plan`."""

    def install(
        self,
        source: str | Path | Mapping[str, Any],
        *,
        output_path: str | Path = DEFAULT_SHADOW_PLAN_PATH,
        receipt_path: str | Path | None = None,
    ) -> dict[str, Any]:
        return install_shadow_plan(
            source,
            output_path=output_path,
            receipt_path=receipt_path,
        )


__all__ = [
    "DEFAULT_SHADOW_DB_PATH",
    "DEFAULT_SHADOW_PLAN_PATH",
    "DEFAULT_SHADOW_RECEIPT_PATH",
    "SHADOW_PLAN_SCHEMA_VERSION",
    "TRIGGER_VERSION",
    "AtomicShadowPlanInstaller",
    "BundleValidationError",
    "ChartScenarioBundleValidator",
    "InstallError",
    "ShadowPlan",
    "install_shadow_plan",
    "load_bundle",
    "read_installed_plan",
    "validate_bundle",
]
