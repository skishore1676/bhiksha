"""Bridge an approved Bhiksha packet to Mala's playbook consultation layer."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from bhiksha.packets.runtime_compile import (
    PacketCompileResult,
    compile_packet_for_runtime,
    load_legacy_retirement_report,
)
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import (  # noqa: E402
    CapabilityManifest,
    ExecutionPacket,
    PacketKind,
    read_packet_file,
)


CommandRunner = Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class ConsultationBridgeResult:
    status: str
    packet_id: str
    packet_version: int
    runtime_mode: str
    symbol: str
    direction: str
    timestamp: str
    chart_read: str
    compile_decision: str
    compile_block_reasons: list[str]
    query_json: str
    query_review: str
    policy_json: str
    policy_card: str
    verdict: str
    policy: str
    selected_exit: str
    allowed_management_policy_ids: list[str]
    artifact_json: str
    artifact_md: str


def consult_mala_playbook(
    *,
    packet_path: Path,
    symbol: str,
    direction: str,
    timestamp: str,
    chart_read: str,
    mala_repo: Path,
    mala_run_dir: Path | None = None,
    mala_python: Path | None = None,
    capability_manifest_path: Path | None = None,
    legacy_retirement_report_path: Path | None = None,
    out_root: Path = Path("artifacts/playbook/consultations"),
    mode: str = "state-management",
    update_mala_log: bool = True,
    runner: CommandRunner | None = None,
) -> ConsultationBridgeResult:
    """Run a packet-authorized consultation against Mala and persist the bridge artifact."""
    if not chart_read.strip():
        raise ValueError("chart_read is required before querying Mala consultation")
    packet = read_packet_file(packet_path)
    if not isinstance(packet, ExecutionPacket):
        raise ValueError(f"expected execution packet, found {packet.kind.value}")
    if packet.source_packet.kind != PacketKind.PLAYBOOK:
        raise ValueError("execution packet must point to a playbook packet")
    normalized_symbol = symbol.strip().upper()
    normalized_direction = _normalize_direction(direction)
    if normalized_symbol not in packet.symbol_scope:
        raise ValueError(f"symbol {normalized_symbol!r} is not in packet scope {packet.symbol_scope!r}")
    compile_result = _compile_packet(
        packet_path=packet_path,
        capability_manifest_path=capability_manifest_path,
        legacy_retirement_report_path=legacy_retirement_report_path,
    )
    if not compile_result.executable:
        raise ValueError(
            "packet is not executable for consultation bridge: "
            + ",".join(compile_result.block_reasons)
        )
    if compile_result.runtime_mode not in {"shadow", "live_approval_gated"}:
        raise ValueError(f"consultation bridge does not support runtime mode {compile_result.runtime_mode!r}")

    effective_run_dir = mala_run_dir or _source_run_dir(packet, mala_repo)
    effective_python = mala_python or mala_repo / ".venv" / "bin" / "python"
    artifact_dir = out_root / _consultation_id(packet, normalized_symbol, normalized_direction, timestamp)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    env = _mala_env(mala_repo)
    run = runner or _run_command

    query_cmd = [
        str(effective_python),
        "-m",
        "src.research.playbook_surface_query",
        "--run-dir",
        str(effective_run_dir),
        "--symbol",
        normalized_symbol,
        "--direction",
        normalized_direction,
        "--timestamp",
        timestamp,
        "--mode",
        mode,
    ]
    if not update_mala_log:
        query_cmd.append("--no-log")
    query_proc = run(query_cmd, mala_repo, env)
    query_values = _parse_key_values(query_proc.stdout)
    query_json = _required_path(query_values, "QUERY_JSON", base=mala_repo)
    query_review = _required_path(query_values, "QUERY_REVIEW", base=mala_repo)

    policy_cmd = [
        str(effective_python),
        "-m",
        "src.research.playbook_policy_card",
        "--query-json",
        str(query_json),
    ]
    if update_mala_log:
        policy_cmd.append("--update-log")
    policy_proc = run(policy_cmd, mala_repo, env)
    policy_values = _parse_key_values(policy_proc.stdout)
    policy_json = _required_path(policy_values, "POLICY_JSON", base=mala_repo)
    policy_card = _required_path(policy_values, "POLICY_CARD", base=mala_repo)

    result = ConsultationBridgeResult(
        status="consulted",
        packet_id=packet.packet_id,
        packet_version=packet.version,
        runtime_mode=compile_result.runtime_mode or "",
        symbol=normalized_symbol,
        direction=normalized_direction,
        timestamp=timestamp,
        chart_read=chart_read.strip(),
        compile_decision=compile_result.decision,
        compile_block_reasons=compile_result.block_reasons,
        query_json=str(query_json),
        query_review=str(query_review),
        policy_json=str(policy_json),
        policy_card=str(policy_card),
        verdict=str(query_values.get("VERDICT", "")),
        policy=str(policy_values.get("POLICY", "")),
        selected_exit=str(policy_values.get("SELECTED_EXIT", "")),
        allowed_management_policy_ids=compile_result.management_policy_ids or [],
        artifact_json=str(artifact_dir / "consultation_bridge.json"),
        artifact_md=str(artifact_dir / "CONSULTATION_BRIDGE.md"),
    )
    _write_bridge_artifacts(result, packet_path=packet_path, artifact_dir=artifact_dir)
    return result


def _compile_packet(
    *,
    packet_path: Path,
    capability_manifest_path: Path | None,
    legacy_retirement_report_path: Path | None,
) -> PacketCompileResult:
    capability_manifest = None
    if capability_manifest_path is not None:
        capability_manifest = CapabilityManifest.model_validate_json(
            capability_manifest_path.read_text(encoding="utf-8")
        )
    legacy_report = None
    if legacy_retirement_report_path is not None:
        legacy_report = load_legacy_retirement_report(legacy_retirement_report_path)
    return compile_packet_for_runtime(
        packet_path,
        capability_manifest=capability_manifest,
        legacy_retirement_report=legacy_report,
    )


def _source_run_dir(packet: ExecutionPacket, mala_repo: Path) -> Path:
    for artifact in packet.lineage.source_artifacts:
        if artifact.label == "run_config":
            path = Path(artifact.uri)
            if not path.is_absolute():
                path = mala_repo / path
            return path.parent
    raise ValueError("execution packet lineage does not include run_config artifact")


def _mala_env(mala_repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    bhiksha_repo = Path(__file__).resolve().parents[3]
    kernel_src = bhiksha_repo.parent / "mala-bhiksha-kernel" / "src"
    entries = [str(kernel_src), str(mala_repo / "src")]
    existing = env.get("PYTHONPATH", "")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _run_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _required_path(values: dict[str, str], key: str, *, base: Path) -> Path:
    raw = values.get(key)
    if not raw:
        raise ValueError(f"command output missing {key}")
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path


def _normalize_direction(direction: str) -> str:
    value = direction.strip().lower()
    if value not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    return value


def _consultation_id(
    packet: ExecutionPacket,
    symbol: str,
    direction: str,
    timestamp: str,
) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamp_slug = re.sub(r"[^A-Za-z0-9]+", "_", timestamp).strip("_")[:48]
    return f"{stamp}_{packet.packet_id}_v{packet.version}_{symbol}_{direction}_{timestamp_slug}"


def _write_bridge_artifacts(
    result: ConsultationBridgeResult,
    *,
    packet_path: Path,
    artifact_dir: Path,
) -> None:
    payload = asdict(result) | {
        "packet_path": str(packet_path),
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }
    artifact_json = Path(result.artifact_json)
    artifact_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(result.artifact_md).write_text(_bridge_markdown(payload), encoding="utf-8")


def _bridge_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Consultation Bridge",
            "",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- runtime_mode: `{payload['runtime_mode']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- timestamp: `{payload['timestamp']}`",
            f"- verdict: `{payload['verdict']}`",
            f"- policy: `{payload['policy']}`",
            f"- selected_exit: `{payload['selected_exit']}`",
            f"- allowed_management_policy_ids: `{', '.join(payload['allowed_management_policy_ids'])}`",
            f"- query_review: `{payload['query_review']}`",
            f"- policy_card: `{payload['policy_card']}`",
            "",
            "## Chart Read",
            "",
            str(payload["chart_read"]),
            "",
        ]
    )
