"""Persistence helpers for Bhiksha launchd status breadcrumbs."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.ops.launchd_registry import job_by_runner, latest_status_path

_STATUS_LOCK_TIMEOUT_SECONDS = 2.0
_STATUS_LOCK_POLL_SECONDS = 0.01


def write_latest_status(repo_root: Path, payload: dict[str, Any]) -> None:
    """Update the compact latest-status snapshot for one launchd runner payload."""
    path = latest_status_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    job_name = str(payload.get("job") or "unknown")
    spec = job_by_runner(job_name)
    recorded = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "label": spec.label if spec else None,
        "payload": payload,
    }
    if (
        spec
        and payload.get("status") == "ok"
        and os.getenv("XPC_SERVICE_NAME") != spec.label
    ):
        recovered = _manual_recovery_watermark(spec.label)
        if recovered:
            recorded["recovered_launchd_failure"] = recovered

    # Atomic replacement keeps readers from seeing a partial document, but it
    # does not prevent two processes from merging the same old snapshot. Hold a
    # stable sibling lock across the complete read/merge/replace transaction.
    with _exclusive_status_lock(path):
        current: dict[str, Any]
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = {}
            current = loaded if isinstance(loaded, dict) else {}
        else:
            current = {}
        jobs = current.get("jobs")
        if not isinstance(jobs, dict):
            jobs = {}
        previous = jobs.get(job_name) if isinstance(jobs.get(job_name), dict) else None
        previous_payload = (
            previous.get("payload") if isinstance(previous, dict) else None
        )
        preserve_unresolved_failure = (
            payload.get("status") == "skipped"
            and payload.get("reason") == "non_trading_day"
            and isinstance(previous_payload, dict)
            and previous_payload.get("status") == "failed"
        )
        if preserve_unresolved_failure:
            jobs[job_name] = {
                **previous,
                "last_skip_at": recorded["recorded_at"],
                "last_skip_payload": payload,
            }
        else:
            jobs[job_name] = recorded
        current.update(
            {
                "generated_at": recorded["recorded_at"],
                "schema": "bhiksha.launchd.latest_status.v1",
                "jobs": jobs,
            }
        )
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(
                json.dumps(current, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


@contextmanager
def _exclusive_status_lock(
    path: Path, *, timeout_seconds: float = _STATUS_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Bound one local status merge without blocking a domain job indefinitely."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring launchd status lock: {lock_path}"
                    )
                time.sleep(_STATUS_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _manual_recovery_watermark(label: str) -> dict[str, str] | None:
    """Bind manual success to the exact idle launchd failure generation."""
    try:
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                capture_output=True, text=True, timeout=3, check=False)
        fields = {}
        for key in ("runs", "last exit code", "state"):
            match = re.search(r"^\s*" + re.escape(key) + r" = (.+)$", result.stdout, re.MULTILINE)
            fields[key] = match.group(1).strip() if match else ""
        if (result.returncode == 0 and fields["runs"].isdigit()
                and fields["state"] == "not running" and fields["last exit code"].isdigit()
                and int(fields["last exit code"]) != 0):
            return {"runs": fields["runs"], "last_exit_code": fields["last exit code"]}
    except (OSError, subprocess.SubprocessError):
        pass
    return None
