import io
import json
import shutil
import sqlite3
import tarfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bhiksha.ops import after_close_backup as backup
from bhiksha.ops.monitoring_snapshot import capture_monitoring_snapshot
from bhiksha.tools.launchd_status import _domain_health, _job_details

NOW = datetime(2026, 10, 14, 23, tzinfo=UTC)
DAY = date(2026, 10, 14)


class FakeDrive:
    def __init__(self):
        self.files = {}
        self.uploads = 0
        self.fail_upload = False
        self.corrupt_download = False

    def folder(self):
        return "private-folder"

    def private(self, file_id):
        assert file_id == "private-folder" or file_id in self.files

    def find(self, parent, name):
        return [v[0] for v in self.files.values() if v[0]["name"] == name]

    def call(self, operation, *args, **kwargs):
        if operation == "upload":
            if self.fail_upload:
                raise RuntimeError("drive_upload_failed_exit_4")
            self.uploads += 1
            data = Path(args[0]).read_bytes()
            name = args[args.index("--name") + 1]
            meta = {"id": "remote-1", "name": name, "size": str(len(data)),
                    "parents": ["private-folder"], "webViewLink": "https://drive.google.com/file/d/remote-1/view"}
            self.files[meta["id"]] = (meta, data)
            return {"file": meta}
        if operation == "get":
            return {"file": self.files[args[0]][0]}
        if operation == "download":
            data = self.files[args[0]][1]
            Path(args[args.index("--out") + 1]).write_bytes(b"bad" if self.corrupt_download else data)
            return {"size": len(data)}
        raise AssertionError(operation)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    for relative in backup.DATABASES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO facts VALUES (7, 'recovery evidence')")
    (root / backup.MARKER).parent.mkdir(parents=True, exist_ok=True)
    (root / backup.MARKER).touch()
    backup.save_json(root / "config/after_close_backup.json", {
        "cleanup_enabled": False, "retain_verified": 2, "reserve_bytes": 1024,
    })
    snapshot = tmp_path / "seed"
    manifest = capture_monitoring_snapshot(root, snapshot)
    for record in manifest["databases"]:
        record["started_at"] = "2026-10-14T20:16:00+00:00"
    backup.save_json(snapshot / "snapshot.json", manifest)
    monkeypatch.setattr(backup, "check_stopped", lambda root: None)
    monkeypatch.setattr(backup.subprocess, "check_output", lambda *a, **k: "commit-test\n")

    def worker(root, action, *args):
        def value(key):
            return args[args.index(key) + 1]
        if action == "manifest":
            return backup.snapshot_manifest(Path(value("--snapshot")), datetime.fromisoformat(value("--cutoff")))
        manifest = backup.read_json(Path(value("--bundle")))
        if action == "archive":
            return {"path": str(backup.make_archive(Path(value("--snapshot")), Path(value("--bundle")).parent, manifest))}
        return backup.verify_restore(Path(value("--archive")), Path(value("--destination")), manifest)
    monkeypatch.setattr(backup, "heavy_worker", worker)
    return root, snapshot, FakeDrive()


def run(setup):
    root, snapshot, drive = setup
    return backup.run_backup(root, snapshot=snapshot, session=DAY, now=NOW, drive=drive)


def test_full_roundtrip_and_second_run_is_idempotent(setup):
    result = run(setup)
    assert result["status"] == "verified"
    assert result["remote_checksum"]["status"] == "verified"
    assert result["restore"]["checksums"] == "all_match"
    assert all(v["max_rowid"]["facts"] == 7 for v in result["restore"]["databases"].values())
    assert result["retention"]["cleanup_enabled"] is False
    assert result["retention"]["deleted_bytes"] == 0
    again = run(setup)
    assert again["reused"] is True
    assert setup[2].uploads == 1
    assert Path(result["snapshot"]).exists()


def test_backup_publication_preserves_independent_stop_proof(setup):
    receipt = {"recorded_at": "2026-10-14T20:10:06+00:00", "status": "ok", "detail": "stopped"}
    backup.save_json(setup[0] / "artifacts/playbook/launchd/latest_status.json", {"jobs": {
        "live-stop": {"payload": {"job": "live-stop", "status": "ok", "session_stop_receipt": receipt}}}})
    setup[2].fail_upload = True
    assert run(setup)["status"] == "failed"
    payload = backup.close_payload(setup[0])
    assert payload["session_stop_receipt"] == receipt
    assert _domain_health(payload)["attention_required"]
    setup[2].fail_upload = False
    assert run(setup)["status"] == "verified"
    assert backup.close_payload(setup[0])["session_stop_receipt"] == receipt


def test_enabled_cleanup_removes_only_verified_scratch_and_is_idempotent(setup, monkeypatch):
    first = run(setup)
    scratch = Path(first["restore"]["path"]).parent
    config_path = setup[0] / "config/after_close_backup.json"
    config = backup.read_json(config_path)
    backup.save_json(config_path, {**config, "cleanup_enabled": True})
    second = run(setup)
    assert second["status"] == "verified"
    assert second["retention"]["status"] == "complete"
    assert second["retention"]["deleted_bytes"] > 0
    assert not scratch.exists()
    assert Path(first["archive"]).exists()
    assert setup[1].exists()
    assert setup[2].uploads == 1
    assert run(setup)["retention"]["deleted_bytes"] == 0


def test_cleanup_refuses_symlinks_and_keeps_verified_status(setup, monkeypatch):
    first = run(setup)
    scratch = Path(first["restore"]["path"]).parent
    (scratch / "foreign").symlink_to(setup[1], target_is_directory=True)
    path = setup[0] / "config/after_close_backup.json"
    backup.save_json(path, {**backup.read_json(path), "cleanup_enabled": True})
    second = run(setup)
    assert second["status"] == "verified"
    assert second["retention"]["status"] == "failed"
    assert scratch.exists() and setup[1].exists()
    assert _domain_health({"job": "live-stop", "status": "ok", "backup": second})["attention_required"]


def test_retention_keeps_two_archives_and_receipts_and_external_captures(setup):
    first = run(setup)
    base = setup[0].parent / "bhiksha-backups"
    original = base / DAY.isoformat()
    for day in ("2026-10-12", "2026-10-13"):
        folder = base / day
        shutil.copytree(original, folder)
        backup.save_json(folder / "receipt.json", {**first, "session": day,
                                                   "archive": str(folder / "recovery.tar.gz")})
    result = backup.apply_retention(base, {"cleanup_enabled": True}, setup[2])
    assert result["status"] == "complete"
    assert not (base / "2026-10-12/recovery.tar.gz").exists()
    assert (base / "2026-10-13/recovery.tar.gz").exists()
    assert Path(first["archive"]).exists()
    assert len(list(base.glob("*/receipt.json"))) == 3
    assert setup[1].exists()


def test_cleanup_remote_change_and_unknown_scratch_block_deletion(setup):
    first = run(setup)
    base = setup[0].parent / "bhiksha-backups"
    scratch = Path(first["restore"]["path"]).parent
    (scratch / "keep-me.txt").write_text("unmanaged")
    result = backup.apply_retention(base, {"cleanup_enabled": True}, setup[2])
    assert result["status"] == "failed" and scratch.exists()
    setup[2].files["remote-1"][0]["modifiedTime"] = "changed"
    result = backup.apply_retention(base, {"cleanup_enabled": True}, setup[2])
    assert result["error"] == "cleanup_remote_identity_changed"
    assert scratch.exists()


def test_last_verified_evidence_survives_stop_projection_overwrite_and_holiday(setup):
    result = run(setup)
    backup.save_json(setup[0] / "artifacts/playbook/launchd/latest_status.json", {"jobs": {"live-stop": {"payload": {"status": "ok"}}}})
    skipped = backup.run_backup(setup[0], session=date(2026, 10, 17), now=NOW, drive=setup[2])
    assert skipped["status"] == "skipped"
    assert skipped["last_verified_backup"] == result["last_verified_backup"]


def test_failed_upload_keeps_completed_archive_and_recovers_without_recapture(setup):
    setup[2].fail_upload = True
    failed = run(setup)
    assert failed["status"] == "failed"
    archive = Path(failed["archive"])
    before = archive.stat().st_mtime_ns
    payload = backup.read_json(setup[0] / "artifacts/playbook/launchd/latest_status.json")["jobs"]["live-stop"]["payload"]
    assert _domain_health(payload)["attention_required"] is True
    assert _job_details({"payload": payload})[0]["backup"]["error"]
    setup[2].fail_upload = False
    recovered = run(setup)
    assert recovered["status"] == "verified"
    assert "error" not in recovered
    assert archive.stat().st_mtime_ns == before
    assert list(archive.parent.glob("failed-attempt-*.json"))
    payload = backup.read_json(setup[0] / "artifacts/playbook/launchd/latest_status.json")["jobs"]["live-stop"]["payload"]
    assert _domain_health(payload)["attention_required"] is False


def test_operator_drawer_fields_expose_timing_checksum_retention_and_recovery(setup):
    result = run(setup)
    details = _job_details({"payload": {"backup": result}})
    # The existing Control Tower renderer reads these scalar fields, not nested JSON.
    visible = " ".join(str(row.get(key, "")) for row in details
                       for key in ("title", "surface", "status", "review_ref"))
    assert result["archive_sha256"] in visible
    assert result["cutoff"] in visible
    assert "cleanup disabled" in visible
    assert "backup-recovery-now" in visible
    assert result["remote"]["webViewLink"] in visible


def test_download_corruption_is_not_verified_and_retry_does_not_duplicate_upload(setup):
    setup[2].corrupt_download = True
    assert run(setup)["status"] == "failed"
    setup[2].corrupt_download = False
    assert run(setup)["status"] == "verified"
    assert setup[2].uploads == 1


def test_failure_attention_remains_open_during_retry_until_verification(setup, monkeypatch):
    records = []
    monkeypatch.setattr(backup, "write_latest_status", lambda root, payload:
                        records.append(json.loads(json.dumps(payload))))
    setup[2].fail_upload = True
    assert run(setup)["unresolved_failure"] is True
    records.clear()
    setup[2].fail_upload = False
    assert run(setup)["unresolved_failure"] is False
    pending = [p for p in records if p["backup"]["status"] != "verified"]
    assert pending and all(_domain_health(p)["attention_required"] for p in pending)
    assert all(not _domain_health(p)["attention_required"] for p in records if p["backup"]["status"] == "verified")


def test_verified_backup_does_not_clear_upstream_chart_failure(setup):
    backup.save_json(setup[0] / "artifacts/playbook/launchd/latest_status.json", {"jobs": {"live-stop": {"payload": {
        "job": "live-stop", "status": "failed", "runtime_stop_status": "ok",
        "chart_evidence": {"status": "failed", "reason": "chart_export_failed"}}}}})
    assert run(setup)["status"] == "verified"
    owner = backup.close_payload(setup[0])
    assert owner["chart_evidence"]["reason"] == "chart_export_failed"
    assert _domain_health(owner)["attention_required"] is True
    assert _domain_health(owner)["reason"] == "close_stage_failed"


def test_stop_failure_stays_failed_even_if_backup_verifies(setup, monkeypatch):
    from types import SimpleNamespace
    from bhiksha.tools import launchd_job as job
    root = setup[0]
    monkeypatch.setattr(job, "_stop_and_export_job", lambda *a, **k: 2)
    monkeypatch.setattr(job, "_print_result", lambda payload: backup.write_latest_status(root, payload))
    def verified(*args, **kwargs):
        assert backup.close_stage_failed(backup.close_payload(root))
        return {"status": "verified"}
    monkeypatch.setattr(backup, "run_backup", verified)
    assert job._stop_job(SimpleNamespace(job="live-stop", backup_only=False), repo_root=root) == 2
    assert _domain_health(backup.close_payload(root))["reason"] == "close_stage_failed"


def test_modified_snapshot_fails_closed(setup):
    (setup[1] / "bhiksha.db").write_bytes(b"corrupted")
    assert run(setup)["status"] == "failed"
    assert setup[2].uploads == 0


def test_incomplete_and_preclose_snapshots_rejected(setup):
    path = setup[1] / "snapshot.json"
    original = backup.read_json(path)
    backup.save_json(path, {**original, "status": "failed"})
    assert run(setup)["status"] == "failed"
    original["databases"][0]["started_at"] = "2026-10-14T19:00:00+00:00"
    backup.save_json(path, original)
    assert run(setup)["status"] == "failed"


def test_active_session_and_holiday_and_early_close_guards():
    with pytest.raises(ValueError):
        backup.check_window(DAY, datetime(2026, 10, 14, 19, tzinfo=UTC))
    with pytest.raises(ValueError):
        backup.check_window(DAY, datetime(2026, 10, 15, 15, tzinfo=UTC))
    with pytest.raises(ValueError):
        backup.check_window(date(2026, 12, 25), datetime(2026, 12, 26, tzinfo=UTC))
    assert backup.check_window(date(2026, 11, 27), datetime(2026, 11, 27, 19, tzinfo=UTC)).hour == 18


def test_child_deadline_stops_before_next_market_open():
    # 08:20 CT: the child gets at most nine minutes, not its usual two hours.
    assert backup.background_timeout(7200, now=datetime(2026, 10, 15, 13, 20, tzinfo=UTC)) == 540
    with pytest.raises(RuntimeError, match="near_market_open"):
        backup.background_timeout(7200, now=datetime(2026, 10, 15, 13, 29, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="during_regular"):
        backup.background_timeout(7200, now=datetime(2026, 10, 15, 14, tzinfo=UTC))


def test_no_cleanup_authority_and_disk_failure(setup, monkeypatch):
    path = setup[0] / "config/after_close_backup.json"
    backup.save_json(path, {"cleanup_enabled": True})
    with pytest.raises(ValueError, match="cleanup"):
        run(setup)
    backup.save_json(path, {"cleanup_enabled": False, "retain_verified": 2, "reserve_bytes": 1024})
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: type("Usage", (), {"free": 0})())
    assert run(setup)["status"] == "failed"
    assert setup[1].exists()
    assert setup[2].uploads == 0


def test_concurrent_capture_refused_and_disabled_does_nothing(setup):
    with backup.exclusive(setup[0].parent / "bhiksha-backups/backup.lock"):
        with pytest.raises(RuntimeError, match="already_running"):
            run(setup)
    (setup[0] / backup.MARKER).unlink()
    assert run(setup)["status"] == "disabled"


@pytest.mark.parametrize("permission", [
    {"type": "anyone", "role": "reader"},
    {"type": "user", "role": "writer", "emailAddress": "other@example.com"},
])
def test_destination_privacy_rejects_other_access(permission):
    drive = backup.Drive({"account": "owner@example.com"})
    drive.call = lambda *args: {"permissions": [permission]}
    with pytest.raises(ValueError, match="private"):
        drive.private("folder")


def test_installed_gog_folder_response_contract():
    drive = backup.Drive({"account": "owner@example.com", "folder_name": "Private backups"})
    def call(op, *args):
        if op == "ls":
            return {"files": []}
        if op == "mkdir":
            return {"folder": {"id": "folder-1", "name": "Private backups"}}
        if op == "get":
            return {"file": {"id": "folder-1", "mimeType": "application/vnd.google-apps.folder"}}
        return {"permissions": [{"type": "user", "role": "owner", "emailAddress": "owner@example.com"}]}
    drive.call = call
    assert drive.folder() == "folder-1"


def test_safe_restore_refuses_extra_members_and_existing_destination(setup, tmp_path):
    result = run(setup)
    manifest = backup.read_json(Path(result["archive"]).parent / "bundle.json")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        item = tarfile.TarInfo("../../escape")
        item.size = 1
        tar.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="members"):
        backup.verify_restore(bad, tmp_path / "isolation", manifest)
    with pytest.raises(FileExistsError):
        backup.verify_restore(Path(result["archive"]), setup[0], manifest)


def test_retention_is_preview_only_keeps_two_verified(tmp_path):
    for n in range(4):
        backup.save_json(tmp_path / f"2026-10-{10+n}/receipt.json", {"status": "verified", "archive_bytes": 50})
    result = backup.retention_preview(tmp_path)
    assert len(result["keep"]) == len(result["would_prune"]) == 2
    assert len(list(tmp_path.glob("*/receipt.json"))) == 4
    assert result["deleted_bytes"] == 0


def test_recovery_action_dispatches_only_backup_and_not_runtime(tmp_path, monkeypatch):
    from bhiksha.tools import launchd_control as control
    calls = []
    monkeypatch.setattr(control.subprocess, "Popen", lambda command, **kwargs:
                        calls.append(command) or type("Process", (), {"pid": 123})())
    result = control.run_control_action(action="backup-recovery-now", repo_root=tmp_path,
                                       action_id="test-recovery")
    assert result["status"] == "dispatched"
    assert calls[0][1:3] == ["live-stop", "--backup-only"]
    assert "stop" not in calls[0] and "restart" not in calls[0]


def test_backup_only_never_calls_stop_export(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bhiksha.tools import launchd_job as job
    (tmp_path / backup.MARKER).parent.mkdir(parents=True)
    (tmp_path / backup.MARKER).touch()
    monkeypatch.setattr(job, "_stop_and_export_job", lambda *a, **k: pytest.fail("must not stop"))
    monkeypatch.setattr(backup, "run_backup", lambda *a, **k: {"status": "verified"})
    results = []
    monkeypatch.setattr(job, "_print_result", results.append)
    assert job._stop_job(SimpleNamespace(job="live-stop", backup_only=True), repo_root=tmp_path) == 0
    assert results[-1]["backup"]["status"] == "verified"
