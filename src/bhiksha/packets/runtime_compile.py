"""Compile shared-kernel packets into Bhiksha runtime eligibility decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    CapabilityManifest,
    ExecutionPacket,
    PacketKind,
    PacketStatus,
    RuntimeCapability,
    read_packet_file,
)


@dataclass(frozen=True, slots=True)
class PacketCompileResult:
    packet_id: str
    version: int
    kind: str
    status: str
    executable: bool
    block_reasons: list[str]
    feature_contract_id: str
    runtime_mode: str | None = None
    management_policy_ids: list[str] | None = None

    @property
    def decision(self) -> str:
        return "take" if self.executable else "block"


def compile_packet_for_runtime(
    packet_path: str | Path,
    *,
    capability_manifest: CapabilityManifest | None = None,
    legacy_retirement_report: dict[str, Any] | None = None,
) -> PacketCompileResult:
    packet = read_packet_file(packet_path)
    block_reasons: list[str] = []
    management_policy_ids = _management_policy_ids(packet)

    if packet.kind != PacketKind.EXECUTION:
        block_reasons.append("packet_kind_not_execution")
    if packet.status != PacketStatus.APPROVED:
        block_reasons.append(f"packet_status_not_approved:{packet.status.value}")
    if packet.operator_approval.status != "approved":
        block_reasons.append("operator_approval_missing")

    runtime_mode = None
    if isinstance(packet, ExecutionPacket):
        runtime_mode = packet.runtime_mode.value
        block_reasons.extend(_capability_blocks(packet, capability_manifest))
        block_reasons.extend(_legacy_retirement_blocks(legacy_retirement_report))

    return PacketCompileResult(
        packet_id=packet.packet_id,
        version=packet.version,
        kind=packet.kind.value,
        status=packet.status.value,
        executable=not block_reasons,
        block_reasons=block_reasons,
        feature_contract_id=packet.feature_contract.contract_id,
        runtime_mode=runtime_mode,
        management_policy_ids=management_policy_ids,
    )


def load_legacy_retirement_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _management_policy_ids(packet: Any) -> list[str]:
    policies = getattr(packet, "management_policies", None)
    if policies is not None:
        return [policy.policy_id for policy in policies]
    if isinstance(packet, ExecutionPacket):
        raw_ids = packet.runtime_controls.get("allowed_management_policy_ids", [])
        if isinstance(raw_ids, list):
            return [str(policy_id) for policy_id in raw_ids if str(policy_id)]
    return []


def _legacy_retirement_blocks(report: dict[str, Any] | None) -> list[str]:
    if report is None:
        return []
    active_count = _safe_int(report.get("active_legacy_wire_count"))
    if str(report.get("status", "")) == "clear" and active_count == 0:
        return []
    if active_count is None:
        return ["legacy_retirement_report_invalid"]
    if active_count > 0:
        return [f"legacy_retirement_blocked:{active_count}"]
    if str(report.get("status", "")) != "clear":
        return [f"legacy_retirement_status:{report.get('status')}"]
    return []


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _capability_blocks(
    packet: ExecutionPacket,
    capability_manifest: CapabilityManifest | None,
) -> list[str]:
    if capability_manifest is None:
        return ["capability_manifest_missing"]
    if not capability_manifest.supports_feature_contract(packet.feature_contract.contract_id):
        return [f"feature_contract_not_supported:{packet.feature_contract.contract_id}"]

    capability = _matching_capability(packet, capability_manifest)
    if capability is None:
        return ["runtime_capability_missing"]
    if not capability.supported:
        return [capability.block_reason or "runtime_capability_not_supported"]
    return []


def _matching_capability(
    packet: ExecutionPacket,
    capability_manifest: CapabilityManifest,
) -> RuntimeCapability | None:
    for capability in capability_manifest.capabilities:
        if packet.feature_contract.contract_id not in capability.feature_contracts:
            continue
        if packet.kind.value not in capability.supported_packet_kinds:
            continue
        if packet.runtime_mode.value not in capability.runtime_modes:
            continue
        if capability.supported_symbols:
            missing_symbols = set(packet.symbol_scope) - set(capability.supported_symbols)
            if missing_symbols:
                continue
        return capability
    return None
