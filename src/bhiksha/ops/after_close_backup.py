"""After-close recovery bundles and verified, owner-scoped local retention."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from uuid import uuid4
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bhiksha.market_data.trading_calendar import CENTRAL, regular_session_bounds
from bhiksha.ops.launchd_status_store import write_latest_status

DATABASES = ("bhiksha.db", "artifacts/observations/exit_edge_live.sqlite3")
SIDECARS = ("artifacts/playbook/active_plan.json",
            "artifacts/observations/exit_edge_live_status.json",
            "artifacts/observations/exit_edge_registration_status.json")
MARKER = "artifacts/playbook/runtime_flags/after_close_backup.enabled"
RECOVERY_SOURCE = ("config/after_close_backup.json", "docs/AFTER_CLOSE_BACKUP.md",
                   "src/bhiksha/ops/after_close_backup.py", "src/bhiksha/tools/after_close_backup.py")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def close_payload(root: Path) -> dict:
    status = read_json(root / "artifacts/playbook/launchd/latest_status.json")
    return ((status.get("jobs") or {}).get("live-stop") or {}).get("payload") or {}


def close_stage_failed(payload: dict) -> bool:
    return bool(payload.get("close_stage_return_code") or
                (payload.get("chart_evidence") or {}).get("status") == "failed")


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("backup_already_running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_window(day: date, now: datetime) -> datetime:
    bounds = regular_session_bounds(day)
    today = regular_session_bounds(now.astimezone(CENTRAL).date())
    if bounds is None or now < bounds[1]:
        raise ValueError("backup_requires_completed_XNYS_session")
    if today and today[0] <= now < today[1]:
        raise ValueError("backup_forbidden_during_regular_session")
    return bounds[1]


def background_timeout(requested: float, *, now: datetime | None = None) -> float:
    """Bound a heavy/network child before the next regular session, even on replay."""
    now = now or datetime.now(UTC)
    day = now.astimezone(CENTRAL).date()
    bounds = regular_session_bounds(day)
    if bounds and bounds[0] <= now < bounds[1]:
        raise RuntimeError("backup_forbidden_during_regular_session")
    openings = [session_bounds[0] for offset in range(15)
                if (session_bounds := regular_session_bounds(day + timedelta(days=offset)))
                and session_bounds[0] > now]
    if not openings:
        raise RuntimeError("next_XNYS_opening_unavailable")
    opening = min(openings)
    available = (opening - now).total_seconds() - 60
    if available < 30:
        raise RuntimeError("backup_deferred_near_market_open")
    return min(requested, available)


def check_stopped(root: Path) -> None:
    proc = subprocess.run([sys.executable, "-m", "bhiksha.tools.server_session", "status"],
                          cwd=root, capture_output=True, text=True, timeout=30)
    # This status command is a process/receipt probe, not analytical live SQL.
    from bhiksha.tools.launchd_job import _parse_runtime_status
    status = _parse_runtime_status(proc.stdout)
    if proc.returncode or not status or status.get("running") is not False:
        raise RuntimeError("backup_requires_verified_stopped_runtime")


def watermarks(path: Path, *, integrity_already_verified: bool = False) -> dict:
    """Only completed snapshot/restore copies enter here; handles close on return."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True)) as db:
        if not integrity_already_verified and db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("backup_database_integrity_failed")
        schema = db.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
        marks = {}
        for name, sql in schema:
            quoted = '"' + name.replace('"', '""') + '"'
            if sql and "WITHOUT ROWID" not in sql.upper():
                marks[name] = db.execute(f"SELECT max(rowid) FROM {quoted}").fetchone()[0]
        return {"integrity": "ok", "max_rowid": marks,
                "schema_sha256": hashlib.sha256(json.dumps(schema).encode()).hexdigest()}


def snapshot_manifest(snapshot: Path, cutoff: datetime) -> dict:
    receipt = read_json(snapshot / "snapshot.json")
    if receipt.get("status") != "complete" or len(receipt.get("databases", [])) != 2:
        raise ValueError("backup_requires_complete_monitoring_snapshot")
    members = {}
    for relative, record in zip(DATABASES, receipt["databases"], strict=True):
        path = snapshot / relative
        # Do not trust arbitrary paths from a receipt. Require the expected pair.
        if (record.get("integrity") != "ok" or
                not str(record.get("source", "")).endswith("/" + relative) or
                datetime.fromisoformat(record["started_at"]) < cutoff):
            raise ValueError("snapshot_not_valid_after_close_evidence")
        if path.is_symlink() or digest(path) != record["sha256"] or path.stat().st_size != record["size_bytes"]:
            raise ValueError("snapshot_checksum_mismatch")
        members[relative] = {"sha256": record["sha256"], "size_bytes": path.stat().st_size,
                             "integrity_evidence": "complete_capture_receipt_and_matched_sha256",
                             "watermarks": watermarks(path, integrity_already_verified=True)}
    for relative, record in receipt.get("sidecars", {}).items():
        if relative not in SIDECARS:
            raise ValueError("snapshot_sidecar_not_allowlisted")
        path = snapshot / relative
        if path.is_symlink() or digest(path) != record["sha256"]:
            raise ValueError("snapshot_sidecar_checksum_mismatch")
        members[relative] = {"sha256": record["sha256"], "size_bytes": path.stat().st_size}
    members["snapshot.json"] = {"sha256": digest(snapshot / "snapshot.json"),
                                "size_bytes": (snapshot / "snapshot.json").stat().st_size}
    return {"schema": "bhiksha.recovery_bundle.v1", "cutoff": cutoff.isoformat(),
            "atomic_across_databases": False, "members": members}


def heavy_worker(root: Path, action: str, *args: str) -> dict:
    """Child lowers its disk priority only after Python has loaded, not launchd dyld."""
    proc = subprocess.run([sys.executable, "-m", "bhiksha.tools.after_close_backup",
                           "--worker", action, *args], cwd=root, capture_output=True,
                          text=True, timeout=background_timeout(7200))
    if proc.returncode:
        raise RuntimeError(f"backup_{action}_worker_failed")
    return json.loads(proc.stdout)


class Drive:
    def __init__(self, config: dict):
        self.config = config

    def call(self, *args: str, timeout: int = 120) -> dict:
        proc = subprocess.run([self.config["gog"], "--client", self.config["client"],
                               "--account", self.config["account"], "--json", "--no-input",
                               "drive", *args], capture_output=True, text=True, timeout=background_timeout(timeout),
                              env={**os.environ, "GOG_KEYRING_BACKEND": "keychain"})
        if proc.returncode:
            # Never persist arbitrary CLI output, which may contain credential details.
            raise RuntimeError(f"drive_{args[0]}_failed_exit_{proc.returncode}")
        return json.loads(proc.stdout)

    def private(self, file_id: str) -> None:
        permissions = self.call("permissions", file_id)
        entries = permissions.get("permissions", [])
        if (not entries or permissions.get("nextPageToken") or any(
                p.get("role") != "owner" or p.get("type") != "user" or
                p.get("emailAddress", "").lower() != self.config["account"].lower()
                for p in entries)):
            raise ValueError("backup_destination_not_private_owner_only")

    def find(self, parent: str, name: str) -> list[dict]:
        query = "name = '" + name.replace("\\", "\\\\").replace("'", "\\'") + "' and trashed = false"
        result = self.call("ls", "--parent", parent, "--query", query, "--max", "100")
        if result.get("nextPageToken"):
            raise ValueError("backup_destination_query_not_unique")
        return result.get("files", [])

    def folder(self) -> str:
        found = self.find("root", self.config["folder_name"])
        if len(found) > 1:
            raise ValueError("duplicate_backup_folders_require_review")
        item = found[0] if found else self.call("mkdir", self.config["folder_name"])["folder"]
        item = self.call("get", item["id"])["file"]
        if item.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError("backup_destination_not_folder")
        self.private(item["id"])
        return item["id"]


def make_archive(snapshot: Path, folder: Path, manifest: dict) -> Path:
    archive = folder / "recovery.tar.gz"
    if archive.exists():
        return archive  # Caller verifies its saved digest before remote reuse.
    partial = folder / "recovery.partial.tar.gz"
    if partial.exists():
        # Keep failed bytes, never silently overwrite incident evidence.
        partial.rename(folder / f"recovery.partial.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.tar.gz")
    save_json(folder / "bundle.json", manifest)
    with tarfile.open(partial, "w:gz", compresslevel=1) as tar:
        for relative in sorted(manifest["members"]):
            source = folder / relative if relative.startswith("recovery_source/") else snapshot / relative
            tar.add(source, arcname=relative, recursive=False)
        tar.add(folder / "bundle.json", arcname="bundle.json", recursive=False)
    partial.replace(archive)
    return archive


def verify_restore(archive: Path, destination: Path, manifest: dict) -> dict:
    """Exclusive new destination; no traversal, links, devices, extras or overwrite."""
    destination.mkdir(parents=True, exist_ok=False)
    expected = set(manifest["members"]) | {"bundle.json"}
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        if set(names) != expected or len(names) != len(expected) or any(not m.isfile() for m in members):
            raise ValueError("restore_archive_members_invalid")
        # Names are matched exactly against a locally allowlisted manifest first.
        for member in members:
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("restore_path_escape")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
    if read_json(destination / "bundle.json") != manifest:
        raise ValueError("restore_manifest_mismatch")
    restored = {}
    for relative, record in manifest["members"].items():
        path = destination / relative
        if path.stat().st_size != record["size_bytes"] or digest(path) != record["sha256"]:
            raise ValueError("restore_checksum_mismatch")
        if relative in DATABASES:
            actual = watermarks(path)
            if actual != record["watermarks"]:
                raise ValueError("restore_watermark_mismatch")
            restored[relative] = actual
    return {"status": "verified", "path": str(destination), "databases": restored,
            "checksums": "all_match", "completed_at": datetime.now(UTC).isoformat()}


def retention_preview(base: Path) -> dict:
    verified = []
    for path in sorted(base.glob("*/receipt.json"), reverse=True):
        record = read_json(path)
        if record.get("status") == "verified":
            verified.append({"receipt": str(path), "snapshot": record.get("snapshot"),
                             "archive_bytes": record.get("archive_bytes", 0)})
    return {"cleanup_enabled": False, "retain_verified": 2, "keep": verified[:2],
            "would_prune": verified[2:], "deleted_bytes": 0}


def apply_retention(base: Path, config: dict, drive: Drive) -> dict:
    """Keep receipts forever; prune only exact managed paths backed by verified Drive bytes.

    Never follow links, remove external snapshots, or delete remote history. A
    persisted per-target journal makes partial deletion/retry observable.
    """
    result = retention_preview(base)
    result.update(cleanup_enabled=config["cleanup_enabled"], status="disabled", deleted=[])
    if not config["cleanup_enabled"]:
        return result
    result["status"] = "complete"
    try:
        if base.is_symlink() or base.resolve() != base.absolute():
            raise ValueError("cleanup_base_not_canonical")
        records = []
        for path in sorted(base.glob("*/receipt.json"), reverse=True):
            if path.parent.name != date.fromisoformat(path.parent.name).isoformat():
                raise ValueError("cleanup_session_invalid")
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("cleanup_receipt_symlink")
            record = read_json(path)
            if record.get("status") == "verified":
                records.append((path, record))
        for index, (receipt, record) in enumerate(records):
            candidates = []
            selected_snapshot = Path(record.get("snapshot") or "")
            managed_snapshot = (
                selected_snapshot.parent == receipt.parent
                and (selected_snapshot.name == "snapshot" or
                     re.fullmatch(r"snapshot-attempt-[0-9a-f]{32}", selected_snapshot.name) is not None)
                and str(selected_snapshot) not in record.get("failed_snapshots", [])
            )
            restore = record.get("restore") or {}
            if restore.get("path"):
                restored = Path(restore["path"])
                scratch = restored.parent
                if (restored.name != "restored" or scratch.parent != base or
                        not scratch.name.startswith("bhiksha-restore-")):
                    raise ValueError("cleanup_restore_not_owned")
                candidates.append(scratch)
            if index >= 2:
                candidates.append(receipt.parent / "recovery.tar.gz")
                # Externally supplied incident/monitoring captures are never ours.
                if managed_snapshot:
                    candidates.append(selected_snapshot)
            candidates = [p for p in candidates if p.exists() or p.is_symlink()]
            if not candidates:
                continue
            if (restore.get("status") != "verified" or restore.get("checksums") != "all_match" or
                    (record.get("remote_checksum") or {}).get("sha256") != record.get("archive_sha256") or
                    (record.get("remote_checksum") or {}).get("status") != "verified"):
                raise ValueError("cleanup_requires_roundtrip_restore_proof")
            remote = drive.call("get", record["remote"]["id"])["file"]
            drive.private(remote["id"])
            drive.private(record["remote_folder_id"])
            if (int(remote.get("size", -1)) != record["archive_bytes"] or
                    any(remote.get(k) != record["remote"].get(k) for k in ("name", "parents", "modifiedTime")) or
                    record["remote_folder_id"] not in remote.get("parents", [])):
                raise ValueError("cleanup_remote_identity_changed")
            for target in candidates:
                check_window(date.fromisoformat(record["session"]), datetime.now(UTC))
                paths = [target, *target.rglob("*")] if target.is_dir() else [target]
                if any(p.is_symlink() or p.resolve() != p.absolute() or
                       not (p.is_file() or p.is_dir()) for p in paths):
                    raise ValueError("cleanup_links_or_special_files_forbidden")
                manifest = read_json(receipt.parent / "bundle.json")
                if not manifest.get("members"):
                    raise ValueError("cleanup_manifest_missing")
                if target.name.startswith("bhiksha-restore-"):
                    expected_files = {record["remote"]["name"], "restored/bundle.json"}
                    expected_files.update("restored/" + name for name in manifest["members"])
                    if {str(p.relative_to(target)) for p in paths if p.is_file()} != expected_files:
                        raise ValueError("cleanup_scratch_contents_changed")
                elif managed_snapshot and target == selected_snapshot:
                    expected_files = {name for name in manifest["members"] if not name.startswith("recovery_source/")}
                    if {str(p.relative_to(target)) for p in paths if p.is_file()} != expected_files:
                        raise ValueError("cleanup_snapshot_contents_changed")
                    # A replacement attempt is owner-managed only for its exact
                    # verified bytes, not just a matching directory prefix.
                    if target.name != "snapshot" and any(
                        (target / name).stat().st_size != manifest["members"][name]["size_bytes"] or
                        digest(target / name) != manifest["members"][name]["sha256"]
                        for name in expected_files
                    ):
                        raise ValueError("cleanup_snapshot_checksum_changed")
                elif target.name == "recovery.tar.gz" and digest(target) != record["archive_sha256"]:
                    raise ValueError("cleanup_archive_checksum_changed")
                # Never race a process still consuming a recovery artifact.
                probe = subprocess.run(["/usr/sbin/lsof", "-t", "+D", str(target)] if target.is_dir()
                                       else ["/usr/sbin/lsof", "-t", str(target)],
                                       capture_output=True, timeout=30)
                if probe.returncode not in (0, 1) or probe.stdout:
                    raise RuntimeError("cleanup_target_open_or_probe_failed")
                size = sum(p.stat().st_size for p in paths if p.is_file())
                journal = receipt.parent / "cleanup-journal.json"
                history = read_json(journal)
                entry = {"path": str(target), "bytes": size, "status": "planned",
                         "recover_from": remote["id"], "at": datetime.now(UTC).isoformat()}
                history.setdefault("attempts", []).append(entry)
                save_json(journal, history)
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                entry["status"] = "deleted"
                save_json(journal, history)
                result["deleted"].append(entry.copy())
                result["deleted_bytes"] += size
    except Exception as exc:
        result.update(status="failed", error=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)
    return result


def last_verified_backup(base: Path) -> dict | None:
    # A successful stop/export writes its own payload before backup starts.
    # Recover continuity from immutable per-session receipts, not that transient
    # latest-status projection, including across holidays and later failures.
    for path in sorted(base.glob("*/receipt.json"), reverse=True):
        pointer = read_json(path).get("last_verified_backup")
        if pointer:
            return pointer
    return None


def preserve_failure(folder: Path, state: dict) -> None:
    key = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:20]
    path = folder / f"failed-attempt-{key}.json"
    if not path.exists():
        save_json(path, state)


def run_backup(root: Path, *, session: date | None = None, snapshot: Path | None = None,
               now: datetime | None = None, drive: Drive | None = None) -> dict:
    root = root.resolve()
    if not (root / MARKER).is_file():
        return {"status": "disabled", "cleanup_enabled": False}
    config = read_json(root / "config/after_close_backup.json")
    if not isinstance(config.get("cleanup_enabled"), bool) or config.get("retain_verified") != 2:
        raise ValueError("cleanup_is_not_authorized")
    now = now or datetime.now(UTC)
    session = session or now.astimezone(CENTRAL).date()
    base = root.parent / "bhiksha-backups"
    if regular_session_bounds(session) is None:
        return {"status": "skipped", "reason": "non_trading_day", "cleanup_enabled": False,
                "last_verified_backup": last_verified_backup(base)}
    cutoff = check_window(session, now)
    check_stopped(root)
    with exclusive(base / "backup.lock"):
        folder = base / session.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        receipt_path = folder / "receipt.json"
        previous = read_json(receipt_path)
        if previous.get("status") == "failed":
            preserve_failure(folder, previous)
        state = {**previous, "schema": "bhiksha.after_close_backup.v1", "session": session.isoformat(),
                 "cutoff": cutoff.isoformat(), "cleanup_enabled": config["cleanup_enabled"],
                 "recovery_command": "python -m bhiksha.tools.after_close_backup --session " + session.isoformat(),
                 "updated_at": now.isoformat(), "attention_key": "bhiksha:after-close-backup"}
        state["unresolved_failure"] = previous.get("status") == "failed" or bool(previous.get("unresolved_failure"))
        if not state.get("last_verified_backup"):
            state["last_verified_backup"] = last_verified_backup(base)
        state.pop("error", None)
        state.pop("failed_phase", None)
        capture_started = False

        def publish(phase: str) -> None:
            if phase not in {"failed", "verified"}:
                check_window(session, datetime.now(UTC))
            state.update(status=phase, updated_at=datetime.now(UTC).isoformat())
            if phase == "failed":
                state["unresolved_failure"] = True
            elif phase == "verified":
                state["unresolved_failure"] = False
            save_json(receipt_path, state)
            upstream = close_payload(root)
            write_latest_status(root, {**upstream, "job": "live-stop", "status": "failed" if close_stage_failed(upstream) else
                                      "ok" if phase == "verified" else "failed" if phase == "failed" else "running",
                                      "runtime_stop_status": "ok", "backup": state})

        try:
            drive = drive or Drive(config)
            publish("preflight")
            # Fail early on authentication/privacy before copying gigabytes.
            remote_folder = drive.folder()
            state["remote_folder_id"] = remote_folder
            if previous.get("status") == "verified":
                remote = drive.call("get", previous["remote"]["id"])["file"]
                drive.private(remote["id"])
                if (int(remote.get("size", -1)) != previous["archive_bytes"] or
                        any(remote.get(key) != previous["remote"].get(key)
                            for key in ("name", "parents", "modifiedTime"))):
                    raise ValueError("verified_remote_identity_changed")
                state.update(previous, reused=True)
                state["cleanup_enabled"] = config["cleanup_enabled"]
                publish("verified")
                state["retention"] = apply_retention(base, config, drive)
                publish("verified")
                return state
            # Immutable completed archives always retain their original capture.
            if snapshot is not None and previous.get("archive_sha256") and str(snapshot) != previous.get("snapshot"):
                raise ValueError("completed_archive_snapshot_cannot_be_replaced")
            chosen = Path(snapshot or previous.get("snapshot") or folder / "snapshot")
            # Preserve the failed capture, never blindly reuse it as a complete
            # snapshot. An explicit supplied snapshot still fails strict checks.
            if snapshot is None and chosen.exists() and read_json(chosen / "snapshot.json").get("status") != "complete":
                state["failed_snapshots"] = list(dict.fromkeys([*state.get("failed_snapshots", []), str(chosen)]))
                chosen = folder / f"snapshot-attempt-{uuid4().hex}"
            state["snapshot"] = str(chosen)
            publish("snapshot")
            if not chosen.exists():
                # Reserve for copy + archive + downloaded archive + extracted copy.
                sizes = sum((root / p).stat().st_size for p in DATABASES)
                if shutil.disk_usage(base).free < sizes * 4 + config["reserve_bytes"]:
                    raise RuntimeError("insufficient_disk_for_capture_and_restore_cleanup_disabled")
                budget = background_timeout(8500)
                capture_started = True
                subprocess.run([sys.executable, "-m", "bhiksha.tools.monitoring_snapshot", "--root", str(root),
                                "--output-dir", str(chosen), "--deadline-seconds", str(max(1, budget - 5))],
                               cwd=root, check=True, timeout=budget,
                               stdout=subprocess.DEVNULL)
            manifest = heavy_worker(root, "manifest", "--snapshot", str(chosen), "--cutoff", cutoff.isoformat())
            manifest["source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            # These allowlisted recovery tools/settings contain no credentials;
            # include their exact bytes without publishing to a public Git remote.
            for relative in RECOVERY_SOURCE:
                source = root / relative
                if source.is_file():
                    name = "recovery_source/" + relative
                    target = folder / name
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                    manifest["members"][name] = {"sha256": digest(target), "size_bytes": target.stat().st_size}
            # Completed archive/manifest are immutable across upload retries, even after a deployment.
            if (folder / "bundle.json").exists():
                saved = read_json(folder / "bundle.json")
                if saved["members"] != manifest["members"] or saved["cutoff"] != manifest["cutoff"]:
                    raise ValueError("existing_bundle_snapshot_changed")
                manifest = saved
            total = sum(m["size_bytes"] for m in manifest["members"].values())
            required = (0 if (folder / "recovery.tar.gz").exists() else total) + config["reserve_bytes"]
            if shutil.disk_usage(base).free < required:
                raise RuntimeError("insufficient_disk_for_restore_cleanup_disabled")
            publish("compressing")
            save_json(folder / "bundle.json", manifest)
            built = heavy_worker(root, "archive", "--snapshot", str(chosen), "--bundle", str(folder / "bundle.json"))
            archive = Path(built["path"])
            if shutil.disk_usage(base).free < total + archive.stat().st_size + config["reserve_bytes"]:
                raise RuntimeError("insufficient_disk_for_download_and_restore_cleanup_disabled")
            sha = digest(archive)
            if previous.get("archive_sha256") and previous["archive_sha256"] != sha:
                raise ValueError("completed_archive_checksum_changed")
            state.update(archive=str(archive), archive_sha256=sha, archive_bytes=archive.stat().st_size)
            name = f"bhiksha-{session.isoformat()}-{sha[:16]}.tar.gz"
            publish("uploading")
            matches = drive.find(remote_folder, name)
            if len(matches) > 1:
                raise ValueError("duplicate_remote_backups_require_review")
            remote = matches[0] if matches else drive.call("upload", str(archive), "--parent", remote_folder,
                                                          "--name", name, timeout=1800)["file"]
            state["remote"] = remote
            publish("remote_verification")
            remote = drive.call("get", remote["id"])["file"]
            drive.private(remote["id"])
            if remote.get("name") != name or int(remote.get("size", -1)) != archive.stat().st_size or remote_folder not in remote.get("parents", []):
                raise ValueError("remote_identity_or_size_mismatch")
            state["remote"] = remote
            # Every newly verified backup must survive a download and isolated restore.
            restore = Path(tempfile.mkdtemp(prefix="bhiksha-restore-", dir=base))
            downloaded = restore / name
            publish("downloading_restore_test")
            drive.call("download", remote["id"], "--out", str(downloaded), timeout=1800)
            if digest(downloaded) != sha:
                raise ValueError("remote_roundtrip_checksum_mismatch")
            publish("restore_integrity")
            state["restore"] = heavy_worker(root, "restore", "--archive", str(downloaded),
                                            "--destination", str(restore / "restored"),
                                            "--bundle", str(folder / "bundle.json"))
            state["remote_checksum"] = {"method": "download_sha256", "sha256": sha, "status": "verified"}
            state["last_verified_at"] = datetime.now(UTC).isoformat()
            state["last_verified_backup"] = {"session": session.isoformat(), "cutoff": state["cutoff"],
                                             "remote": remote, "archive_sha256": sha,
                                             "verified_at": state["last_verified_at"],
                                             "restore_path": state["restore"]["path"]}
            state.pop("error", None)
            publish("verified")
            state["retention"] = apply_retention(base, config, drive)
            publish("verified")
            return state
        except Exception as exc:
            phase = state.get("status", "preflight")
            state["failed_phase"] = phase
            if phase == "snapshot" and capture_started:
                path = Path(state["snapshot"]) / "snapshot.json"
                captured = read_json(path)
                if captured.get("schema") == "bhiksha.monitoring_snapshot.v1" and captured.get("status") != "complete":
                    state["failed_phase"] = "snapshot:" + str((captured.get("progress") or {}).get("phase", "capture"))
                    captured.update(status="failed", error_type=captured.get("error_type") or type(exc).__name__,
                                    failed_phase=state["failed_phase"], terminated_at=datetime.now(UTC).isoformat())
                    save_json(path, captured)
            state["error"] = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            state["recovery_action"] = "Retry owner command after resolving auth/disk/error; completed snapshot/archive are reused."
            publish("failed")
            preserve_failure(folder, state)
            return state
