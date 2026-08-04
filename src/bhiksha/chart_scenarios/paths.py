"""Resolved path confinement for the experiment-only shadow namespace."""

from __future__ import annotations

from pathlib import Path


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


__all__ = ["require_experiment_path"]
