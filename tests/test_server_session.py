from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.tools.server_session import main as server_session_main


def test_server_session_sync_uses_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active_plan_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    sync_log_dir = tmp_path / "artifacts" / "playbook" / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_PATH", str(active_plan_path))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(sync_log_dir))

    def _fake_sync(**kwargs):
        assert kwargs["spreadsheet_id"] == "spreadsheet123"
        return _sync_result(active_plan_path, sync_log_dir / "active_plan_sync_2026-04-09.jsonl")

    monkeypatch.setattr("bhiksha.tools.server_session.sync_active_plan_once", _fake_sync)

    exit_code = server_session_main(["sync"])

    assert exit_code == 0


def test_server_session_start_status_stop_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "runtime" / "bhiksha.pid"
    runtime_log_dir = tmp_path / "runtime_logs"
    active_plan_path = tmp_path / "active_plan.json"
    active_plan_path.write_text("{}", encoding="utf-8")

    class _FakeProcess:
        pid = 43210

    popen_calls: list[list[str]] = []
    running_pids = {43210}
    kill_calls: list[tuple[int, int]] = []

    def _fake_popen(command, **kwargs):
        popen_calls.append(command)
        assert kwargs["cwd"] == str(tmp_path.resolve())
        assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
        return _FakeProcess()

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig != 0:
            running_pids.discard(pid)
        elif pid not in running_pids:
            raise ProcessLookupError

    monkeypatch.setattr("bhiksha.tools.server_session.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("bhiksha.tools.server_session.os.kill", _fake_kill)

    exit_code = server_session_main(
        [
            "start",
            "--pid-path",
            str(pid_path),
            "--runtime-log-dir",
            str(runtime_log_dir),
            "--active-plan",
            str(active_plan_path),
            "--repo-root",
            str(tmp_path),
            "--python-executable",
            "/tmp/python",
            "--live",
        ]
    )

    assert exit_code == 0
    metadata = json.loads(pid_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == 43210
    assert metadata["live"] is True
    assert popen_calls == [[
        "/tmp/python",
        "-u",
        "-m",
        "bhiksha.tools.trade_session",
        "--active-plan",
        str(active_plan_path.resolve()),
        "--live",
    ]]

    exit_code = server_session_main(["status", "--pid-path", str(pid_path)])
    assert exit_code == 0

    exit_code = server_session_main(["stop", "--pid-path", str(pid_path), "--timeout-seconds", "0.1"])
    assert exit_code == 0
    assert not pid_path.exists()
    assert any(sig != 0 for _, sig in kill_calls)


def test_server_session_restart_syncs_stops_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "runtime" / "bhiksha.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(
        json.dumps(
            {
                "pid": 11111,
                "log_path": str(tmp_path / "old.log"),
                "active_plan_path": str(tmp_path / "active_plan.json"),
                "live": True,
            }
        ),
        encoding="utf-8",
    )

    running_pids = {11111, 22222}
    started_commands: list[list[str]] = []

    def _fake_sync(**kwargs):
        return _sync_result(tmp_path / "active_plan.json", tmp_path / "sync.log")

    class _FakeProcess:
        pid = 22222

    def _fake_popen(command, **kwargs):
        started_commands.append(command)
        assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
        return _FakeProcess()

    def _fake_kill(pid: int, sig: int) -> None:
        if sig != 0:
            running_pids.discard(pid)
        elif pid not in running_pids:
            raise ProcessLookupError

    monkeypatch.setattr("bhiksha.tools.server_session.sync_active_plan_once", _fake_sync)
    monkeypatch.setattr("bhiksha.tools.server_session.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("bhiksha.tools.server_session.os.kill", _fake_kill)

    exit_code = server_session_main(
        [
            "restart",
            "--google-sheet-id",
            "spreadsheet123",
            "--credentials-path",
            str(tmp_path / "credentials.json"),
            "--pid-path",
            str(pid_path),
            "--runtime-log-dir",
            str(tmp_path / "runtime_logs"),
            "--active-plan",
            str(tmp_path / "active_plan.json"),
            "--repo-root",
            str(tmp_path),
            "--python-executable",
            "/tmp/python",
        ]
    )

    assert exit_code == 0
    assert pid_path.exists()
    metadata = json.loads(pid_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == 22222
    assert started_commands == [[
        "/tmp/python",
        "-u",
        "-m",
        "bhiksha.tools.trade_session",
        "--active-plan",
        str((tmp_path / "active_plan.json").resolve()),
    ]]


def test_server_session_ensure_running_starts_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "runtime" / "bhiksha.pid"
    runtime_log_dir = tmp_path / "runtime_logs"
    active_plan_path = tmp_path / "active_plan.json"
    active_plan_path.write_text("{}", encoding="utf-8")

    class _FakeProcess:
        pid = 54321

    def _fake_popen(command, **kwargs):
        assert kwargs["cwd"] == str(tmp_path.resolve())
        return _FakeProcess()

    def _fake_kill(pid: int, sig: int) -> None:
        del pid, sig
        raise ProcessLookupError

    monkeypatch.setattr("bhiksha.tools.server_session.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("bhiksha.tools.server_session.os.kill", _fake_kill)

    exit_code = server_session_main(
        [
            "ensure-running",
            "--pid-path",
            str(pid_path),
            "--runtime-log-dir",
            str(runtime_log_dir),
            "--active-plan",
            str(active_plan_path),
            "--repo-root",
            str(tmp_path),
            "--python-executable",
            "/tmp/python",
            "--live",
        ]
    )

    assert exit_code == 0
    metadata = json.loads(pid_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == 54321


def test_server_session_ensure_running_noops_when_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "runtime" / "bhiksha.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(json.dumps({"pid": 11111}), encoding="utf-8")

    def _fake_kill(pid: int, sig: int) -> None:
        assert pid == 11111
        assert sig == 0

    monkeypatch.setattr("bhiksha.tools.server_session.os.kill", _fake_kill)

    exit_code = server_session_main(
        [
            "ensure-running",
            "--pid-path",
            str(pid_path),
            "--runtime-log-dir",
            str(tmp_path / "runtime_logs"),
            "--active-plan",
            str(tmp_path / "active_plan.json"),
            "--repo-root",
            str(tmp_path),
            "--python-executable",
            "/tmp/python",
        ]
    )

    assert exit_code == 0


def _sync_result(active_plan_path: Path, log_path: Path):
    from bhiksha.tools.sync_active_plan import SyncActivePlanResult

    return SyncActivePlanResult(
        active_plan_path=active_plan_path.resolve(),
        active_plan_id="active_plan_2026-04-09",
        summary={"deployment_count": 2, "suppressed_count": 0},
        suppressed=[],
        changed=True,
        log_path=log_path.resolve(),
        attempt=1,
    )
