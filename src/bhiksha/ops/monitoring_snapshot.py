"""Bounded online backups for analysis; never hold live cursors while computing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


# The 3.3 GiB production copy took 24 minutes at variable background I/O rates.
# Leave headroom for slower sessions and growth without raising disk priority.
DEFAULT_TIMEOUT_SECONDS = 3600


def snapshot_database(source: Path, destination: Path, *,
                      timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                      deadline: float | None = None,
                      on_progress: Callable[[dict], None] | None = None) -> dict:
    """Copy through SQLite, including WAL contents, without changing source pragmas.

    WAL sources use a pinned read view (writers can continue); rollback-journal
    sources release their read lock after each small backup step. The progress
    callback runs after that lock is released. All source handles close before
    integrity checks, hashing or caller analysis. Timeout is checked between
    steps, not a hard deadline for filesystem I/O inside a SQLite call.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("snapshot must differ from source")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents clobbering previous evidence or a live DB.
    with destination.open("xb"):
        pass
    steps = 0
    busy_steps = 0
    last_report = float("-inf")

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("monitoring snapshot total deadline exceeded")

    def report(phase: str, *, force: bool = False, **details) -> None:
        nonlocal last_report
        now = time.monotonic()
        if on_progress is not None and (force or now - last_report >= 5):
            on_progress({"source": str(source), "snapshot": str(destination),
                         "phase": phase, "updated_at": datetime.now(UTC).isoformat(),
                         "elapsed_seconds": round(now - started, 3),
                         "timeout_seconds": timeout_seconds, "steps": steps,
                         "busy_steps": busy_steps, **details})
            last_report = now

    try:
        check_deadline()
        report("opening_source", force=True)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0)) as src:
            while True:
                try:
                    journal = src.execute("PRAGMA journal_mode").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    check_deadline()
                    if getattr(exc, "sqlite_errorcode", 0) & 255 not in (
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED
                    ):
                        raise
                    if time.monotonic() - started > timeout_seconds:
                        raise TimeoutError("monitoring snapshot source remained busy") from exc
                    report("waiting_for_source")
                    time.sleep(0.01)
            if journal == "wal":
                src.execute("BEGIN")
                src.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            pages = 256 if journal == "wal" else 8

            def progress(status: int, remaining: int, total: int) -> None:
                nonlocal steps, busy_steps
                steps += 1
                check_deadline()
                busy_steps += status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                report("copying", remaining_pages=remaining, total_pages=total,
                       copied_pages=total - remaining)
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError("monitoring snapshot deadline exceeded")
                if status == sqlite3.SQLITE_OK:
                    # Yield between successful steps too (backup's sleep only
                    # handles BUSY/LOCKED). No read lock is held here for DELETE.
                    time.sleep(0.001)

            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst, pages=pages, progress=progress, sleep=0.01)
        source_closed_at = datetime.now(UTC).isoformat()
        report("validating", force=True, source_closed_at=source_closed_at)
        check_deadline()
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as check:
            validation_vm_steps = 0

            def validation_progress() -> int:
                nonlocal validation_vm_steps
                validation_vm_steps += 10000
                report("validating", source_closed_at=source_closed_at,
                       validation_vm_steps=validation_vm_steps)
                return int(deadline is not None and time.monotonic() >= deadline)

            check.set_progress_handler(validation_progress, 10000)
            try:
                checked = check.execute("PRAGMA quick_check").fetchall()
            except sqlite3.OperationalError:
                check_deadline()
                raise
            if checked != [("ok",)]:
                raise ValueError("snapshot quick_check failed")
        report("hashing", force=True, source_closed_at=source_closed_at)
        digest = hashlib.sha256()
        hashed_bytes = 0
        with destination.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                check_deadline()
                digest.update(block)
                hashed_bytes += len(block)
                report("hashing", hashed_bytes=hashed_bytes, source_closed_at=source_closed_at)
        check_deadline()
        return {
            "source": str(source), "snapshot": str(destination),
            "started_at": started_at, "source_closed_at": source_closed_at,
            "completed_at": datetime.now(UTC).isoformat(), "journal_mode": journal,
            "pages_per_step": pages, "steps": steps, "busy_steps": busy_steps,
            "size_bytes": destination.stat().st_size, "sha256": digest.hexdigest(),
            "integrity": "ok",
        }
    except BaseException:
        # Incomplete bytes are evidence, never an eligible analytical snapshot.
        # A later attempt must use a fresh directory, not overwrite these files.
        raise


def capture_monitoring_snapshot(root: Path, output_dir: Path, *,
                                timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                                deadline_seconds: float | None = None) -> dict:
    """Make a fresh evidence directory. No broker, credentials or scheduler access."""
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ValueError("snapshot deadline must be positive")
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {"schema": "bhiksha.monitoring_snapshot.v1", "status": "capturing",
               "atomic_across_databases": False, "databases": [], "sidecars": {}}
    receipt_path = output_dir / "snapshot.json"
    def save_receipt() -> None:
        temporary = receipt_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.replace(receipt_path)

    def progress(details: dict) -> None:
        receipt["progress"] = details
        save_receipt()

    save_receipt()
    try:
        for relative in ("bhiksha.db", "artifacts/observations/exit_edge_live.sqlite3"):
            receipt["databases"].append(snapshot_database(
                root / relative, output_dir / relative, timeout_seconds=timeout_seconds,
                deadline=deadline,
                on_progress=progress))
            save_receipt()
        for relative in ("artifacts/playbook/active_plan.json",
                         "artifacts/observations/exit_edge_live_status.json",
                         "artifacts/observations/exit_edge_registration_status.json"):
            path = root / relative
            if path.exists():
                data = path.read_bytes()
                json.loads(data)  # Fail visibly on a torn or invalid sidecar.
                target = output_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                receipt["sidecars"][relative] = {"sha256": hashlib.sha256(data).hexdigest(),
                                                 "captured_at": datetime.now(UTC).isoformat()}
        receipt["status"] = "complete"
        receipt.pop("progress", None)
    except BaseException as exc:
        receipt.update(status="failed", error_type=type(exc).__name__,
                       failed_phase=(receipt.get("progress") or {}).get("phase", "capture"))
        raise
    finally:
        save_receipt()
    return receipt
