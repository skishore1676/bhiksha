from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.packets.runtime_compile import compile_packet_for_runtime
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    CapabilityManifest,
    ExecutionPacket,
    FeatureContract,
    FeatureSpec,
    ManagementPolicy,
    OperatorApproval,
    PacketKind,
    PacketLineage,
    PacketRef,
    PacketStatus,
    PlaybookPacket,
    RuntimeCapability,
    RuntimeMode,
    SourceArtifact,
    write_packet,
)


@pytest.fixture(autouse=True)
def _default_parity_report(tmp_path: Path) -> None:
    _write_parity_report(tmp_path)


def test_playbook_packet_validates_but_fails_closed_for_execution(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _playbook_packet())

    result = compile_packet_for_runtime(packet_path)

    assert result.executable is False
    assert result.decision == "block"
    assert result.eligibility == "blocked"
    assert "packet_kind_not_execution" in result.block_reasons
    assert "operator_approval_missing" in result.block_reasons
    assert result.management_policy_ids == ["reversal_extreme__fixed_1_5r"]


def test_execution_packet_blocks_when_feature_contract_is_not_supported(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())
    manifest = CapabilityManifest(
        manifest_id="bhiksha.test",
        feature_contracts=[],
        capabilities=[],
    )

    result = compile_packet_for_runtime(packet_path, capability_manifest=manifest)

    assert result.executable is False
    assert result.block_reasons == [
        "feature_contract_not_supported:mean_reversion_at_extremes_intraday_v1"
    ]


def test_execution_packet_compiles_when_manifest_supports_contract(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())
    contract = _feature_contract()
    manifest = CapabilityManifest(
        manifest_id="bhiksha.test",
        feature_contracts=[contract],
        capabilities=[
            RuntimeCapability(
                capability_id="mean_reversion_at_extremes_intraday_v1",
                label="Mean reversion runtime adapter",
                supported=True,
                supported_packet_kinds=["execution"],
                supported_symbols=["IWM", "QQQ"],
                feature_contracts=[contract.contract_id],
                runtime_modes=["shadow"],
            )
        ],
    )

    result = compile_packet_for_runtime(packet_path, capability_manifest=manifest)

    assert result.executable is True
    assert result.decision == "take"
    assert result.eligibility == "eligible"
    assert result.block_reasons == []
    assert result.management_policy_ids == ["reversal_extreme__fixed_1_5r"]


def test_execution_packet_blocks_when_legacy_retirement_report_is_not_clear(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(),
        legacy_retirement_report={
            "status": "blocked",
            "active_legacy_wire_count": 8,
        },
    )

    assert result.executable is False
    assert "legacy_retirement_blocked:8" in result.block_reasons


def test_execution_packet_blocks_when_parity_report_is_not_passed(tmp_path: Path) -> None:
    _write_parity_report(tmp_path, status="failed")
    packet_path = write_packet(tmp_path, _execution_packet())

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(),
    )

    assert result.executable is False
    assert "parity_report_not_passed:failed" in result.block_reasons


def test_execution_packet_blocks_when_parity_report_id_is_fake(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(),
    )

    assert result.executable is True

    _write_parity_report(tmp_path, report_id="parity.fake")
    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(),
    )

    assert result.executable is False
    assert "parity_report_id_mismatch" in result.block_reasons


def test_shadow_execution_packet_requires_shadow_only_controls(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _execution_packet(
            runtime_controls={
                "allowed_management_policy_ids": ["reversal_extreme__fixed_1_5r"],
                "live_automated_allowed": True,
            }
        ),
    )

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(),
    )

    assert result.executable is False
    assert "shadow_only_control_missing" in result.block_reasons
    assert "live_automated_not_allowed_for_shadow" in result.block_reasons
    assert "operator_management_policy_selection_missing" in result.block_reasons


def test_live_approval_gated_packet_requires_live_controls(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _execution_packet(
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
            runtime_controls={
                "allowed_management_policy_ids": ["reversal_extreme__fixed_1_5r"],
                "management_policy_specs": _management_policy_specs(),
                "operator_must_select_management_policy": True,
                "shadow_only": False,
                "live_automated_allowed": False,
                "live_ticket_required": True,
                "management_policy_specs_required": True,
                "requires_underlying_stop_price": True,
                "live_management_required": True,
            },
        ),
    )

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(runtime_modes=["shadow", "live_approval_gated"]),
    )

    assert result.executable is True
    assert result.runtime_mode == "live_approval_gated"
    assert result.block_reasons == []


def test_live_approval_gated_packet_blocks_without_management_specs(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _execution_packet(
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
            runtime_controls={
                "allowed_management_policy_ids": ["reversal_extreme__fixed_1_5r"],
                "operator_must_select_management_policy": True,
                "shadow_only": False,
                "live_automated_allowed": False,
                "live_ticket_required": True,
            },
        ),
    )

    result = compile_packet_for_runtime(
        packet_path,
        capability_manifest=_supporting_manifest(runtime_modes=["shadow", "live_approval_gated"]),
    )

    assert result.executable is False
    assert "management_policy_specs_required_missing" in result.block_reasons
    assert "underlying_stop_price_requirement_missing" in result.block_reasons
    assert "live_management_requirement_missing" in result.block_reasons


def _playbook_packet() -> PlaybookPacket:
    return PlaybookPacket(
        packet_id="playbook.mean_reversion_at_extremes.iwm_qqq",
        version=1,
        status=PacketStatus.REVIEW,
        title="IWM/QQQ Mean Reversion At Extremes",
        symbol_scope=["IWM", "QQQ"],
        intended_horizon="intraday-short-horizon",
        feature_contract=_feature_contract(),
        lineage=_lineage(),
        playbook_id="mean-reversion-at-extremes-intraday",
        management_policies=[
            ManagementPolicy(
                policy_id="reversal_extreme__fixed_1_5r",
                name="reversal_extreme / fixed_1_5r",
                rank=1,
            )
        ],
        consultation_state="ready_for_parity",
    )


def _execution_packet(
    runtime_controls: dict | None = None,
    *,
    runtime_mode: RuntimeMode = RuntimeMode.SHADOW,
) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
        version=1,
        status=PacketStatus.APPROVED,
        title="IWM/QQQ Mean Reversion Execution",
        symbol_scope=["IWM", "QQQ"],
        intended_horizon="intraday-short-horizon",
        feature_contract=_feature_contract(),
        lineage=_lineage(),
        operator_approval=OperatorApproval(status="approved", actor="operator"),
        source_packet=PacketRef(
            packet_id="playbook.mean_reversion_at_extremes.iwm_qqq",
            version=1,
            kind=PacketKind.PLAYBOOK,
        ),
        runtime_mode=runtime_mode,
        capability_manifest_id="bhiksha.test",
        parity_report_id="parity.mean_reversion.test",
        runtime_controls=runtime_controls
        or {
            "allowed_management_policy_ids": ["reversal_extreme__fixed_1_5r"],
            "shadow_only": True,
            "live_automated_allowed": False,
            "operator_must_select_management_policy": True,
        },
    )


def _supporting_manifest(runtime_modes: list[str] | None = None) -> CapabilityManifest:
    return _supporting_manifest_with_modes(runtime_modes or ["shadow"])


def _supporting_manifest_with_modes(runtime_modes: list[str]) -> CapabilityManifest:
    contract = _feature_contract()
    return CapabilityManifest(
        manifest_id="bhiksha.test",
        feature_contracts=[contract],
        capabilities=[
            RuntimeCapability(
                capability_id="mean_reversion_at_extremes_intraday_v1",
                label="Mean reversion runtime adapter",
                supported=True,
                supported_packet_kinds=["execution"],
                supported_symbols=["IWM", "QQQ"],
                feature_contracts=[contract.contract_id],
                runtime_modes=runtime_modes,
            )
        ],
    )


def _management_policy_specs() -> dict:
    return {
        "reversal_extreme__fixed_1_5r": {
            "policy_id": "reversal_extreme__fixed_1_5r",
            "stop_family": "reversal_extreme",
            "stop_anchor": "underlying_reversal_extreme",
            "exit_family": "fixed_1_5r",
            "target_model": "fixed_r",
            "target_r": 1.5,
            "hard_flat_time_et": "15:55",
            "option_stop_fallback_pct": 0.45,
            "target_order_mode": "virtual_or_broker",
        }
    }


def _feature_contract() -> FeatureContract:
    return FeatureContract(
        contract_id="mean_reversion_at_extremes_intraday_v1",
        bar_interval="1m",
        session="rth",
        provider="polygon",
        warmup_bars=60,
        features=[FeatureSpec(name="opening_vwap_rth", provider_sensitive=True)],
    )


def _lineage() -> PacketLineage:
    return PacketLineage(
        source_system="mala_v2",
        source_artifacts=[
            SourceArtifact(label="test", uri="data/results/playbooks/test"),
            SourceArtifact(label="parity_report", uri="PARITY_REPORT.json"),
        ],
    )


def _write_parity_report(tmp_path: Path, *, status: str = "passed", report_id: str = "parity.mean_reversion.test") -> Path:
    path = tmp_path / "PARITY_REPORT.json"
    path.write_text(
        json.dumps(
            {
                "report_id": report_id,
                "packet_ref": {
                    "packet_id": "playbook.mean_reversion_at_extremes.iwm_qqq",
                    "version": 1,
                    "kind": "playbook",
                },
                "status": status,
            }
        ),
        encoding="utf-8",
    )
    return path
