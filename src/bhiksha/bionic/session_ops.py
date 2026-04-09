"""Helpers for active-plan prep plus optional Mala interoperability."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from bhiksha.ops.summary import SessionSummary


def default_mala_root(repo_root: str | Path) -> Path:
    return (Path(repo_root).resolve().parent / "mala_v1").resolve()


def default_active_plan_path(repo_root: str | Path) -> Path:
    return (Path(repo_root).resolve() / "artifacts" / "playbook" / "active_plan.json").resolve()


def default_prepare_out_dir(mala_root: str | Path, *, today: date | None = None) -> Path:
    session_date = today or datetime.now(UTC).date()
    return (Path(mala_root).resolve() / "data" / "results" / "active_session" / session_date.isoformat() / "pre_open").resolve()


def default_feedback_export_dir(mala_root: str | Path, session_id: str) -> Path:
    return (Path(mala_root).resolve() / "data" / "live_feedback" / session_id).resolve()


def resolve_mala_python(mala_root: str | Path, explicit_python: str | None = None) -> str:
    if explicit_python:
        return explicit_python
    candidate = Path(mala_root).resolve() / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def build_compile_command(
    *,
    mala_root: str | Path,
    out_dir: str | Path,
    live_authorized: bool,
    publish_bhiksha: bool,
    mala_python: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    root = Path(mala_root).resolve()
    command = [
        resolve_mala_python(root, mala_python),
        "scripts/compile_active_session.py",
        "--out-dir",
        str(Path(out_dir).resolve()),
    ]
    if live_authorized:
        command.append("--live-authorized")
    if publish_bhiksha:
        command.append("--publish-bhiksha")
    if extra_args:
        command.extend(extra_args)
    return command


def run_compile_command(
    *,
    mala_root: str | Path,
    out_dir: str | Path,
    live_authorized: bool,
    publish_bhiksha: bool,
    mala_python: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = build_compile_command(
        mala_root=mala_root,
        out_dir=out_dir,
        live_authorized=live_authorized,
        publish_bhiksha=publish_bhiksha,
        mala_python=mala_python,
        extra_args=extra_args,
    )
    return subprocess.run(
        command,
        cwd=str(Path(mala_root).resolve()),
        check=True,
        text=True,
        capture_output=True,
    )


def parse_compile_stdout(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def session_summary_to_dict(summary: SessionSummary) -> dict[str, Any]:
    payload = asdict(summary)
    payload["recent_events"] = [asdict(item) for item in summary.recent_events]
    return payload


def write_feedback_bundle(
    *,
    repo_root: str | Path,
    active_plan_path: str | Path,
    summary: SessionSummary,
    observation_packets: list[dict[str, Any]],
    export_to_mala_root: str | Path | None = None,
) -> Path:
    resolved_repo_root = Path(repo_root).resolve()
    active_plan = Path(active_plan_path).resolve()
    plan = json.loads(active_plan.read_text(encoding="utf-8"))
    active_plan_id = str(plan.get("active_plan_id") or active_plan.stem)
    bundle_root = resolved_repo_root / "artifacts" / "playbook" / "session_feedback" / active_plan_id
    bundle_root.mkdir(parents=True, exist_ok=True)

    summary_path = bundle_root / "session_summary.json"
    summary_path.write_text(
        json.dumps(session_summary_to_dict(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    observations_dir = bundle_root / "observations"
    observations_dir.mkdir(parents=True, exist_ok=True)
    for packet in observation_packets:
        deployment_id = str(packet.get("deployment_id") or "unknown")
        (observations_dir / f"{deployment_id}.json").write_text(
            json.dumps(packet, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (bundle_root / "observation_index.json").write_text(
        json.dumps({"reports": observation_packets}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(active_plan, bundle_root / "active_plan.json")

    if export_to_mala_root is not None:
        target = default_feedback_export_dir(export_to_mala_root, active_plan_id)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(bundle_root, target)

    return bundle_root
