import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bhiksha.ops import monitoring_snapshot as capture
from bhiksha.tools.launchd_status import _job_findings
from test_after_close_backup import setup, backup, DAY, NOW  # existing isolated fake-Drive fixture
from test_after_close_backup import run as run_fake_backup


def test_supplied_complete_snapshot_can_replace_failed_unbundled_attempt(setup):
    root, seed, drive = setup
    folder = root.parent / "bhiksha-backups" / DAY.isoformat()
    bad = folder / "snapshot"
    backup.save_json(bad / "snapshot.json", {"status": "capturing"})
    backup.save_json(folder / "receipt.json", {"status": "failed", "snapshot": str(bad)})
    result = backup.run_backup(root, snapshot=seed, session=DAY, now=NOW, drive=drive)
    assert result["status"] == "verified"
    assert result["snapshot"] == str(seed)
    assert (bad / "snapshot.json").read_text() == '{\n  "status": "capturing"\n}\n'


def test_managed_incomplete_capture_is_preserved_and_fresh_attempt_selected(setup, monkeypatch):
    root, seed, drive = setup
    folder = root.parent / "bhiksha-backups" / DAY.isoformat()
    bad = folder / "snapshot"
    backup.save_json(bad / "snapshot.json", {"status": "capturing"})
    (bad / "partial.db").write_bytes(b"preserved failure")
    backup.save_json(folder / "receipt.json", {"status": "failed", "snapshot": str(bad)})
    calls = []
    original_run = backup.subprocess.run

    def run(command, **kwargs):
        if "--output-dir" not in command:
            return original_run(command, **kwargs)
        calls.append((command, kwargs))
        destination = Path(command[command.index("--output-dir") + 1])
        shutil.copytree(seed, destination)
    monkeypatch.setattr(backup.subprocess, "run", run)
    result = backup.run_backup(root, session=DAY, now=NOW, drive=drive)
    assert result["status"] == "verified", result
    assert result["snapshot"] != str(bad)
    assert result["failed_snapshots"] == [str(bad)]
    assert (bad / "partial.db").read_bytes() == b"preserved failure"
    command, kwargs = calls[0]
    assert float(command[command.index("--deadline-seconds") + 1]) < kwargs["timeout"] <= 8500


def test_hard_child_timeout_marks_capture_terminal_and_preserves_progress(setup, monkeypatch):
    root, seed, drive = setup

    def run(command, **kwargs):
        destination = Path(command[command.index("--output-dir") + 1])
        backup.save_json(destination / "snapshot.json", {
            "schema": "bhiksha.monitoring_snapshot.v1", "status": "capturing",
            "progress": {"phase": "validating", "source_closed_at": "original"}})
        (destination / "partial.db").write_bytes(b"failed bytes")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(backup.subprocess, "run", run)
    result = backup.run_backup(root, session=DAY, now=NOW, drive=drive)
    assert result["status"] == "failed" and result["unresolved_failure"]
    assert result["failed_phase"] == "snapshot:validating"
    receipt = backup.read_json(Path(result["snapshot"]) / "snapshot.json")
    assert receipt["status"] == "failed" and receipt["error_type"] == "TimeoutExpired"
    assert receipt["progress"]["source_closed_at"] == "original"
    assert (Path(result["snapshot"]) / "partial.db").read_bytes() == b"failed bytes"
    assert drive.uploads == 0


@pytest.mark.parametrize("phase", ["validating", "hashing"])
def test_existing_owner_total_deadline_applies_after_source_closure(tmp_path, monkeypatch, phase):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
        db.execute("INSERT INTO facts VALUES ('test')")
    clock = [0.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
    progress = []

    def observe(value):
        progress.append(value)
        if value["phase"] == phase:
            assert value["source_closed_at"]
            clock[0] = 101.0
    target = tmp_path / "copy.db"
    with pytest.raises(TimeoutError, match="total deadline"):
        capture.snapshot_database(source, target, deadline=100.0, on_progress=observe)
    assert target.exists()


def test_backup_needs_human_is_not_an_entry_reconciliation_finding():
    findings = _job_findings({"domain": {"ok": False, "status": "needs_human",
                                         "reason": "after_close_backup", "attention_required": True}})
    assert "Private recovery backup failed" in findings[0]
    ordinary = _job_findings({"domain": {"ok": False, "status": "needs_human"}})
    assert "Entry reconciliation could not finish safely" in ordinary[0]


def test_sqlite_validation_callback_interrupts_at_owner_deadline(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
        db.executemany("INSERT INTO facts VALUES (?)", [("synthetic",)] * 10000)
    clock = [0.0]
    validating = [False]
    def monotonic():
        if validating[0]:
            clock[0] += 6.0  # Cross the ordinary five-second progress throttle.
        return clock[0]
    monkeypatch.setattr(capture.time, "monotonic", monotonic)
    steps = []
    def observe(value):
        if value.get("phase") == "validating":
            validating[0] = True
        if value.get("validation_vm_steps"):
            steps.append(value["validation_vm_steps"])
            clock[0] = 101.0
    with pytest.raises(TimeoutError, match="total deadline"):
        capture.snapshot_database(source, tmp_path / "copy.db", deadline=100.0, on_progress=observe)
    assert steps


@pytest.mark.parametrize("kind", ["managed", "external", "failed", "unknown_name", "modified", "symlink"])
def test_retention_only_prunes_verified_selected_owned_replacement(setup, kind):
    first = run_fake_backup(setup)
    root, seed, drive = setup
    base = root.parent / "bhiksha-backups"
    original = base / DAY.isoformat()
    selections = {}
    failed_paths = []
    for day in ("2026-10-12", "2026-10-13"):
        folder = base / day
        shutil.copytree(original, folder)
        selected = folder / ("snapshot-attempt-" + "a" * 32)
        shutil.copytree(seed, selected)
        failed = folder / ("snapshot-attempt-" + "b" * 32)
        failed.mkdir()
        (failed / "partial.db").write_bytes(b"preserve failed evidence")
        failed_paths.append(failed)
        failed_refs = [str(failed)]
        if day == "2026-10-12":
            if kind == "external":
                selected = seed
            elif kind == "failed":
                failed_refs.append(str(selected))
            elif kind == "unknown_name":
                selected.rename(folder / "snapshot-attempt-unowned")
                selected = folder / "snapshot-attempt-unowned"
            elif kind == "modified":
                (selected / "bhiksha.db").write_bytes(b"unique modified evidence")
            elif kind == "symlink":
                # Synthetic-only relocation; never touches a real owner capture.
                moved = folder / "keep-original"
                selected.rename(moved)
                selected.symlink_to(seed, target_is_directory=True)
        selections[day] = selected
        backup.save_json(folder / "receipt.json", {**first, "session": day,
            "archive": str(folder / "recovery.tar.gz"), "snapshot": str(selected),
            "failed_snapshots": failed_refs})
    result = backup.apply_retention(base, {"cleanup_enabled": True}, drive)
    if kind in {"modified", "symlink"}:
        assert result["status"] == "failed"
    else:
        assert result["status"] == "complete"
    assert selections["2026-10-12"].exists() is (kind != "managed")
    assert selections["2026-10-13"].exists()  # newest two verified sessions retained
    assert seed.exists()
    assert all((p / "partial.db").read_bytes() == b"preserve failed evidence" for p in failed_paths)
