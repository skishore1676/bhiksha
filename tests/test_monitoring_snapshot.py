import sqlite3
import threading
import time
from contextlib import closing

import pytest

from bhiksha.ops.monitoring_snapshot import capture_monitoring_snapshot, snapshot_database


def database(path, journal="delete"):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA journal_mode={journal}")
    connection.execute("CREATE TABLE facts (value INTEGER)")
    connection.execute("INSERT INTO facts VALUES (1)")
    connection.commit()
    return connection


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_snapshot_includes_committed_data_and_long_analysis_cannot_lock_source(tmp_path, journal):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    with closing(database(source, journal)) as live:
        live.execute("INSERT INTO facts VALUES (2)")
        live.commit()  # In WAL mode this data need not be in the main DB file.
        result = snapshot_database(source, target)
        with closing(sqlite3.connect(target)) as analysis:
            analysis.execute("BEGIN")
            assert analysis.execute("SELECT * FROM facts").fetchall() == [(1,), (2,)]
            with closing(sqlite3.connect(source, timeout=0.001)) as writer:
                for i in range(3, 103):
                    writer.execute("INSERT INTO facts VALUES (?)", (i,))
                    writer.commit()
            assert analysis.execute("SELECT count(*) FROM facts").fetchone()[0] == 2
        assert result["integrity"] == "ok"
        assert result["journal_mode"] == journal


def test_busy_source_times_out_without_publishing_incomplete_snapshot(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    with closing(database(source)) as live:
        live.execute("BEGIN EXCLUSIVE")
        # journal-mode probe may itself fail immediately; either failure is safe.
        with pytest.raises((sqlite3.OperationalError, TimeoutError)):
            snapshot_database(source, target, timeout_seconds=0.03)
    assert target.exists()  # Failed bytes stay preserved, never certified complete.


def test_never_overwrites_source_or_prior_evidence(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    database(source).close()
    original = source.read_bytes()
    with pytest.raises(ValueError):
        snapshot_database(source, source)
    target.write_bytes(b"prior evidence")
    with pytest.raises(FileExistsError):
        snapshot_database(source, target)
    assert source.read_bytes() == original
    assert target.read_bytes() == b"prior evidence"


def test_deadline_aborts_backup_and_missing_source_records_failure(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    database(source).close()
    with pytest.raises(TimeoutError):
        snapshot_database(source, target, timeout_seconds=0.000000001)
    assert target.exists()
    output = tmp_path / "failed-capture"
    with pytest.raises(sqlite3.OperationalError):
        capture_monitoring_snapshot(tmp_path / "missing-runtime", output)
    import json
    assert json.loads((output / "snapshot.json").read_text())["status"] == "failed"


def test_capture_manifest_is_explicit_about_independent_watermarks(tmp_path):
    root, output = tmp_path / "runtime", tmp_path / "snapshot"
    database(root / "bhiksha.db", "wal").close()
    database(root / "artifacts/observations/exit_edge_live.sqlite3").close()
    result = capture_monitoring_snapshot(root, output)
    assert result["status"] == "complete"
    assert result["atomic_across_databases"] is False
    assert len(result["databases"]) == 2
    assert all(x["sha256"] and x["source_closed_at"] for x in result["databases"])
    with pytest.raises(FileExistsError):
        capture_monitoring_snapshot(root, output)


def test_incremental_backup_allows_writes_between_steps(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    live = database(source)
    live.execute("CREATE TABLE padding (data BLOB)")
    live.executemany("INSERT INTO padding VALUES (?)", [(b"x" * 4000,)] * 300)
    live.commit()
    live.close()
    stop, failures, writes = threading.Event(), [], []

    def write():
        with closing(sqlite3.connect(source, timeout=0.1)) as writer:
            while not stop.is_set():
                try:
                    writer.execute("INSERT INTO facts VALUES (3)")
                    writer.commit()
                    writes.append(1)
                except sqlite3.OperationalError as exc:
                    failures.append(str(exc))
                time.sleep(0.02)

    thread = threading.Thread(target=write)
    thread.start()
    try:
        # Continuous rollback-journal writes can restart a backup; this capture
        # must stay bounded rather than pin a source transaction to finish.
        try:
            snapshot_database(source, target, timeout_seconds=0.3)
        except TimeoutError:
            assert target.exists()
    finally:
        stop.set()
        thread.join(timeout=2)
    assert writes and not failures


def test_cli_backgrounds_before_reading_databases(monkeypatch, tmp_path):
    from bhiksha.tools import monitoring_snapshot as cli

    calls = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kw: calls.append((command, kw)))

    def capture(*args, **kwargs):
        assert calls[0][0] == ["/usr/sbin/taskpolicy", "-b", "-p", str(cli.os.getpid())]
        assert calls[0][1]["check"] is True
        return {"status": "complete"}

    monkeypatch.setattr(cli, "capture_monitoring_snapshot", capture)
    assert cli.main(["--output-dir", str(tmp_path / "capture")]) == 0


def test_cli_background_failure_prevents_capture(monkeypatch, tmp_path):
    from bhiksha.tools import monitoring_snapshot as cli

    monkeypatch.setattr(cli.sys, "platform", "darwin")

    def fail(*args, **kwargs):
        raise cli.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cli.subprocess, "run", fail)
    monkeypatch.setattr(cli, "capture_monitoring_snapshot",
                        lambda *a, **kw: pytest.fail("must not capture at normal priority"))
    with pytest.raises(cli.subprocess.CalledProcessError):
        cli.main(["--output-dir", str(tmp_path / "capture")])


def test_progress_manifest_is_readable_during_copy_and_preserved_on_failure(monkeypatch, tmp_path):
    import json
    from bhiksha.ops import monitoring_snapshot as module

    output = tmp_path / "snapshot"

    def slow_copy(source, destination, *, timeout_seconds, on_progress, deadline=None):
        assert timeout_seconds == 3600
        on_progress({"phase": "copying", "copied_pages": 256, "total_pages": 900000})
        manifest = json.loads((output / "snapshot.json").read_text())
        assert manifest["status"] == "capturing"
        assert manifest["progress"]["copied_pages"] == 256
        raise TimeoutError("bounded copy expired")

    monkeypatch.setattr(module, "snapshot_database", slow_copy)
    with pytest.raises(TimeoutError):
        module.capture_monitoring_snapshot(tmp_path / "runtime", output)
    manifest = json.loads((output / "snapshot.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["progress"]["total_pages"] == 900000
    assert not (output / "snapshot.json.tmp").exists()


def test_progress_marks_source_closed_before_validation_and_hashing(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    database(source).close()
    progress = []
    snapshot_database(source, target, on_progress=progress.append)
    phases = [item["phase"] for item in progress]
    assert phases[0] == "opening_source"
    assert phases[-2:] == ["validating", "hashing"]
    assert all(item["source_closed_at"] for item in progress[-2:])


def test_long_validation_reports_activity_after_source_closure(monkeypatch, tmp_path):
    from itertools import count
    from bhiksha.ops import monitoring_snapshot as module

    source, target = tmp_path / "live.db", tmp_path / "copy.db"
    with closing(database(source)) as live:
        live.executemany("INSERT INTO facts VALUES (?)", ((i,) for i in range(5000)))
        live.commit()
    # Simulate a slow scan without sleeping or relying on host disk speed.
    clock = count(0, 6)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    progress = []
    result = module.snapshot_database(source, target, on_progress=progress.append)
    validation = [p for p in progress if p.get("validation_vm_steps")]
    assert result["integrity"] == "ok" and validation
    assert all(p["source_closed_at"] for p in validation)
    assert [p["validation_vm_steps"] for p in validation] == sorted(
        p["validation_vm_steps"] for p in validation)
