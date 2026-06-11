"""Report which code revision the runtime is executing."""

from __future__ import annotations

from pathlib import Path
import subprocess

_REPO_ROOT = Path(__file__).resolve().parents[3]


def code_version_snapshot(repo_root: Path | None = None) -> dict[str, object]:
    """Return the git commit and dirty state of the running source tree.

    Never raises: a runtime started outside a git checkout reports unknown.
    """
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except Exception as exc:
        return {"git_commit": "unknown", "git_dirty": None, "error": str(exc)[:200]}
    return {"git_commit": commit, "git_dirty": bool(status)}
