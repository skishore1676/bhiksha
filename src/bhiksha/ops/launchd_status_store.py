"""Persistence helpers for Bhiksha launchd status breadcrumbs."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from bhiksha.ops.launchd_registry import job_by_runner, latest_status_path


def write_latest_status(repo_root: Path, payload: dict[str, Any]) -> None:
    """Update the compact latest-status snapshot for one launchd runner payload."""
    path = latest_status_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    job_name = str(payload.get("job") or "unknown")
    spec = job_by_runner(job_name)
    recorded = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "label": spec.label if spec else None,
        "payload": payload,
    }
    previous = jobs.get(job_name) if isinstance(jobs.get(job_name), dict) else None
    previous_payload = previous.get("payload") if isinstance(previous, dict) else None
    preserve_auth_failure = (
        job_name == "schwab-refresh"
        and payload.get("status") == "skipped"
        and isinstance(previous_payload, dict)
        and previous_payload.get("status") == "failed"
    )
    if preserve_auth_failure:
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
