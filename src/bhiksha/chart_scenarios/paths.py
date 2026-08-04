"""Resolved path confinement for the experiment-only shadow namespace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class RunArtifactPaths:
    root: Path
    plan: Path
    install_receipt: Path
    database: Path
    live_cycle_input: Path
    cycle_receipts: Path
    events_export: Path
    projection_receipt: Path


def run_artifact_paths(
    campaign_id: str,
    run_id: str,
    *,
    root: str | Path = "artifacts/chart_scenarios/runs",
) -> RunArtifactPaths:
    """Resolve an immutable run-owned evidence namespace and SQLite chain."""

    for label, value in (("campaign_id", campaign_id), ("run_id", run_id)):
        if _IDENTITY_RE.fullmatch(value) is None:
            raise ValueError(f"{label} is not safe for a run artifact path")
    run_root = require_experiment_path(
        Path(root) / campaign_id / run_id, role="run artifact root"
    )
    return RunArtifactPaths(
        root=run_root,
        plan=run_root / "active_shadow_plan.json",
        install_receipt=run_root / "install.receipt.json",
        database=run_root / "shadow_events.sqlite3",
        live_cycle_input=run_root / "live_cycle_input.json",
        cycle_receipts=run_root / "cycles",
        events_export=run_root / "events.json",
        projection_receipt=run_root / "sheet-projection.receipt.json",
    )


def require_experiment_path(path: str | Path, *, role: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    parts = resolved.parts
    confined = any(
        parts[index : index + 2] == ("artifacts", "chart_scenarios")
        for index in range(max(0, len(parts) - 1))
    )
    if not confined:
        raise ValueError(
            f"chart-scenario {role} must be under artifacts/chart_scenarios: {resolved}"
        )
    return resolved


__all__ = ["RunArtifactPaths", "require_experiment_path", "run_artifact_paths"]
