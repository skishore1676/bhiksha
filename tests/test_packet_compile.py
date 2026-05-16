from __future__ import annotations

from pathlib import Path

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


def test_playbook_packet_validates_but_fails_closed_for_execution(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _playbook_packet())

    result = compile_packet_for_runtime(packet_path)

    assert result.executable is False
    assert result.decision == "block"
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


def _execution_packet() -> ExecutionPacket:
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
        runtime_mode=RuntimeMode.SHADOW,
        capability_manifest_id="bhiksha.test",
        parity_report_id="parity.mean_reversion.test",
        runtime_controls={
            "allowed_management_policy_ids": ["reversal_extreme__fixed_1_5r"],
        },
    )


def _supporting_manifest() -> CapabilityManifest:
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
                runtime_modes=["shadow"],
            )
        ],
    )


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
            SourceArtifact(label="test", uri="data/results/playbooks/test")
        ],
    )
