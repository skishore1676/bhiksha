from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bhiksha.packets.consultation_bridge import consult_mala_playbook
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    CapabilityManifest,
    ExecutionPacket,
    FeatureContract,
    FeatureSpec,
    OperatorApproval,
    PacketKind,
    PacketLineage,
    PacketRef,
    PacketStatus,
    RuntimeCapability,
    RuntimeMode,
    SourceArtifact,
    write_packet,
)


def test_consultation_bridge_requires_chart_read(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())

    with pytest.raises(ValueError, match="chart_read is required"):
        consult_mala_playbook(
            packet_path=packet_path,
            symbol="IWM",
            direction="short",
            timestamp="2026-05-11 09:40 America/Chicago",
            chart_read=" ",
            mala_repo=tmp_path / "mala_v2",
        )


def test_consultation_bridge_fails_closed_when_packet_does_not_compile(tmp_path: Path) -> None:
    packet_path = write_packet(tmp_path, _execution_packet())
    calls: list[list[str]] = []

    with pytest.raises(ValueError, match="packet is not executable"):
        consult_mala_playbook(
            packet_path=packet_path,
            symbol="IWM",
            direction="short",
            timestamp="2026-05-11 09:40 America/Chicago",
            chart_read="stretched and failing from the chart",
            mala_repo=tmp_path / "mala_v2",
            runner=_fake_runner(calls),
        )

    assert calls == []


def test_consultation_bridge_runs_mala_query_and_writes_bhiksha_artifact(tmp_path: Path) -> None:
    mala_repo = tmp_path / "mala_v2"
    mala_repo.mkdir()
    packet_path = write_packet(tmp_path, _execution_packet())
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(_supporting_manifest().model_dump_json(), encoding="utf-8")
    calls: list[list[str]] = []

    result = consult_mala_playbook(
        packet_path=packet_path,
        symbol="iwm",
        direction="SHORT",
        timestamp="2026-05-11 09:40 America/Chicago",
        chart_read="price stretched above VWAP and started rejecting the push",
        mala_repo=mala_repo,
        mala_python=Path("/tmp/fake-mala-python"),
        capability_manifest_path=manifest_path,
        out_root=tmp_path / "consultations",
        runner=_fake_runner(calls),
    )

    assert len(calls) == 2
    assert calls[0][1:3] == ["-m", "src.research.playbook_surface_query"]
    assert calls[1][1:3] == ["-m", "src.research.playbook_policy_card"]
    assert result.status == "consulted"
    assert result.symbol == "IWM"
    assert result.direction == "short"
    assert result.verdict == "take"
    assert result.policy == "take"
    assert result.selected_exit == "vwap_return"
    assert result.allowed_management_policy_ids == ["reversal_extreme__fixed_1r"]

    artifact = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert artifact["packet_id"] == "execution.mean_reversion_at_extremes.iwm_qqq"
    assert artifact["chart_read"] == "price stretched above VWAP and started rejecting the push"
    assert artifact["policy"] == "take"
    assert artifact["selected_exit"] == "vwap_return"
    assert Path(result.artifact_md).exists()


def _fake_runner(calls: list[list[str]]):
    def run(
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert str(cwd / "src") in env["PYTHONPATH"]
        if "src.research.playbook_surface_query" in cmd:
            query_dir = cwd / _run_dir() / "surface_queries" / "fake_query"
            query_dir.mkdir(parents=True)
            (query_dir / "query_result.json").write_text("{}", encoding="utf-8")
            (query_dir / "QUERY_REVIEW.md").write_text("# Query\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "QUERY_REVIEW="
                    f"{_run_dir()}/surface_queries/fake_query/QUERY_REVIEW.md\n"
                    "QUERY_JSON="
                    f"{_run_dir()}/surface_queries/fake_query/query_result.json\n"
                    "VERDICT=take\n"
                    "ACTIVE_MATCHES=7\n"
                ),
            )
        policy_dir = cwd / _run_dir() / "surface_queries" / "fake_query"
        (policy_dir / "policy_card.json").write_text("{}", encoding="utf-8")
        (policy_dir / "POLICY_CARD.md").write_text("# Policy\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "POLICY_CARD="
                f"{_run_dir()}/surface_queries/fake_query/POLICY_CARD.md\n"
                "POLICY_JSON="
                f"{_run_dir()}/surface_queries/fake_query/policy_card.json\n"
                "POLICY=take\n"
                "SELECTED_EXIT=vwap_return\n"
            ),
        )

    return run


def _execution_packet() -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
        version=1,
        status=PacketStatus.APPROVED,
        title="IWM/QQQ Mean Reversion Execution",
        symbol_scope=["IWM", "QQQ"],
        intended_horizon="intraday-short-horizon",
        feature_contract=_feature_contract(),
        lineage=PacketLineage(
            source_system="mala_v2",
            source_artifacts=[
                SourceArtifact(label="run_config", uri=str(_run_dir() / "run_config.json"))
            ],
        ),
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
            "allowed_management_policy_ids": ["reversal_extreme__fixed_1r"],
            "shadow_only": True,
            "live_automated_allowed": False,
            "operator_must_select_management_policy": True,
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


def _run_dir() -> Path:
    return Path("data/results/playbooks/mean_reversion_at_extremes/test_run")
