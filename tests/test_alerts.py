import subprocess
from pathlib import Path

from bhiksha.ops.alerts import _default_lathi_invocation, send_lathi_alert


def test_send_lathi_alert_invokes_telegram_notify_with_redaction(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='{"live_send_requested": true, "network_call_performed": true, "body": "code=secret-code"}',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(
        title="Token failed",
        body='access_token="secret-access"\nrefresh_token="secret-refresh"\ncode=secret-code',
        level="error",
        mode="live",
        profile="jarvis-northstar",
        command=["lathi-bus"],
    )

    assert result.ok is True
    assert result.network_call_performed is True
    assert result.attempted is True
    args = calls[0][0]
    assert args[:5] == ["lathi-bus", "telegram-notify", "--profile", "jarvis-northstar", "--title"]
    assert args[args.index("--title") + 1].startswith("🚨🚨🚨 BHIKSHA FAILURE")
    assert "--live" in args
    body = args[args.index("--body") + 1]
    assert "🔴🔴🔴 ACTION REQUIRED 🔴🔴🔴" in body
    assert "🚨 BHIKSHA FAILURE - DO NOT IGNORE" in body
    assert "secret-access" not in body
    assert "secret-refresh" not in body
    assert "secret-code" not in body
    assert "secret-code" not in result.stdout_tail


def test_send_lathi_alert_spool_mode_does_not_use_live_flag(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(
        title="Spool only",
        body="Body",
        mode="spool",
        command=["python3", "-m", "lathi_bus.cli"],
    )

    assert result.ok is True
    assert calls[0][:4] == ["python3", "-m", "lathi_bus.cli", "telegram-notify"]
    assert "--live" not in calls[0]


def test_info_alert_is_not_decorated(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(
        title="Routine test",
        body="Routine body",
        level="info",
        mode="spool",
        command=["lathi-bus"],
    )

    assert result.ok is True
    assert calls[0][calls[0].index("--title") + 1] == "Routine test"
    assert calls[0][calls[0].index("--body") + 1] == "Routine body"


def test_send_lathi_alert_passes_telegram_presentation_flags(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(
        title="Session report",
        body="Quick read",
        level="info",
        mode="spool",
        profile="beacon",
        command=["lathi-bus"],
        template="status",
        fields={"Status": "GREEN", "token": "access_token=secret-token"},
        link_preview="disabled",
    )

    assert result.ok is True
    args = calls[0]
    assert args[args.index("--template") + 1] == "status"
    assert args[args.index("--link-preview") + 1] == "disabled"
    assert "--field" in args
    assert "Status=GREEN" in args
    assert "secret-token" not in " ".join(args)


def test_send_lathi_alert_live_mode_requires_network_call(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='{"live_send_requested": false, "network_call_performed": false}',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = send_lathi_alert(
        title="Live but unconfigured",
        body="Body",
        mode="live",
        command=["lathi-bus"],
    )

    assert result.ok is False
    assert result.return_code == 0
    assert result.network_call_performed is False


def test_default_lathi_invocation_prefers_checkout_venv(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "lathi-bus"
    package = repo / "lathi_bus"
    package.mkdir(parents=True)
    (package / "cli.py").write_text("")
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CMD", raising=False)
    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CWD", raising=False)
    monkeypatch.setattr("bhiksha.ops.alerts.shutil.which", lambda name: None)

    command, cwd = _default_lathi_invocation(repo)

    assert command == [str(python), "-m", "lathi_bus.cli"]
    assert cwd == repo


def test_default_lathi_invocation_can_fall_back_to_python3(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "lathi-bus"
    (repo / "lathi_bus").mkdir(parents=True)
    (repo / "lathi_bus" / "cli.py").write_text("")

    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CMD", raising=False)
    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CWD", raising=False)
    monkeypatch.setattr("bhiksha.ops.alerts.shutil.which", lambda name: None)

    command, cwd = _default_lathi_invocation(Path(repo))

    assert command == ["python3", "-m", "lathi_bus.cli"]
    assert cwd == repo


def test_default_lathi_invocation_prefers_home_checkout_over_path_wrapper(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    repo = home / "code" / "lathi-bus"
    (repo / "lathi_bus").mkdir(parents=True)
    (repo / "lathi_bus" / "cli.py").write_text("")
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CMD", raising=False)
    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CWD", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("bhiksha.ops.alerts.shutil.which", lambda name: "/usr/local/bin/lathi-bus")

    command, cwd = _default_lathi_invocation()

    assert command == [str(python), "-m", "lathi_bus.cli"]
    assert cwd == repo
