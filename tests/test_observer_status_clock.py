from datetime import UTC, datetime, timedelta
import json

import pytest

from bhiksha.ops.launchd_registry import job_by_runner
from bhiksha.tools import launchd_status


def _receipt(tmp_path, health):
    marker = tmp_path / "artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled"
    marker.parent.mkdir(parents=True)
    marker.touch()
    path = tmp_path / "artifacts/observations/exit_edge_live_status.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(health))
    return path


@pytest.mark.parametrize("explicit_now", [False, True])
def test_snapshot_clock_does_not_predate_a_receipt_read(
    tmp_path, monkeypatch, explicit_now,
):
    start = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    completed = start + timedelta(seconds=2)
    clock = {"now": start}
    path = _receipt(tmp_path, {
        "role": "observer", "ready": True, "worker_alive": True,
        "updated_at": (start + timedelta(seconds=1)).isoformat(),
        "collection_state": "idle_market_closed",
    })

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    original_read = launchd_status._read_json

    def read_json(candidate):
        result = original_read(candidate)
        if candidate == path:
            clock["now"] = completed
        return result

    monkeypatch.setattr(launchd_status, "datetime", Clock)
    monkeypatch.setattr(launchd_status, "_read_json", read_json)
    monkeypatch.setattr(launchd_status, "registered_launchd_jobs",
                        lambda: [job_by_runner("exit-edge-observer")])
    monkeypatch.setattr(launchd_status, "_launchd_state",
                        lambda **kw: {"com.bhiksha.exit-edge-observer": {"loaded": True}})
    monkeypatch.setattr(launchd_status, "_runtime_status", lambda **kw: {})
    monkeypatch.setattr(launchd_status, "inspect_provider_reconciliation", lambda path: {})

    result = launchd_status.build_status_snapshot(
        repo_root=tmp_path, active_plan_path=tmp_path / "plan.json",
        now=start if explicit_now else None,
    )
    job = result["jobs"][0]
    assert result["generated_at"] == start.isoformat()
    assert job["last_run_status"] == (
        "observer_heartbeat_stale" if explicit_now else "idle_market_closed"
    )
    assert job["lifecycle"] == ("stuck" if explicit_now else "armed")


@pytest.mark.parametrize("override, expected", [
    ({"age": 90}, "healthy"),
    ({"age": 91}, "observer_heartbeat_stale"),
    ({"age": -1}, "observer_heartbeat_stale"),
    ({"updated_at": "invalid"}, "observer_heartbeat_stale"),
    ({"ready": False}, "observer_not_ready"),
    ({"worker_alive": False}, "observer_not_ready"),
    ({"oldest_pending_write_seconds": 181}, "observer_writer_stalled"),
    ({"collection_state": "observing", "observation_polls": 6,
      "quote_age": 91}, "observer_quote_evidence_stale"),
])
def test_read_time_clock_preserves_observer_safety_guards(
    tmp_path, monkeypatch, override, expected,
):
    now = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)
    changes = dict(override)
    age = changes.pop("age", 0)
    quote_age = changes.pop("quote_age", 0)
    _receipt(tmp_path, {
        "role": "observer", "ready": True, "worker_alive": True,
        "updated_at": (now - timedelta(seconds=age)).isoformat(),
        "last_quote_persisted_at": (now - timedelta(seconds=quote_age)).isoformat(),
        **changes,
    })

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(launchd_status, "datetime", Clock)
    result = launchd_status._exit_edge_observer_status(tmp_path, {"loaded": True})
    assert result["status"] == expected
    assert result["ok"] is (expected == "healthy")
