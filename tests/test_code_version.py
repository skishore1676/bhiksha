"""Tests for the code-version snapshot helper."""

from __future__ import annotations

from pathlib import Path
import subprocess

from bhiksha.ops.code_version import code_version_snapshot


def test_code_version_reports_commit_and_clean_state(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    snapshot = code_version_snapshot(tmp_path)
    assert len(snapshot["git_commit"]) == 40
    assert snapshot["git_dirty"] is False

    (tmp_path / "file.txt").write_text("changed", encoding="utf-8")
    dirty_snapshot = code_version_snapshot(tmp_path)
    assert dirty_snapshot["git_dirty"] is True


def test_code_version_handles_non_repo(tmp_path: Path) -> None:
    snapshot = code_version_snapshot(tmp_path / "not_a_repo")
    assert snapshot["git_commit"] == "unknown"
    assert snapshot["git_dirty"] is None
