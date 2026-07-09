import subprocess
import sys
from pathlib import Path

from bhiksha.ops.alerts import (
    _default_lathi_invocation,
    publish_lathi_review,
    send_lathi_alert,
)


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


def test_default_lathi_invocation_falls_back_to_current_python(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "lathi-bus"
    (repo / "lathi_bus").mkdir(parents=True)
    (repo / "lathi_bus" / "cli.py").write_text("")

    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CMD", raising=False)
    monkeypatch.delenv("BHIKSHA_LATHI_BUS_CWD", raising=False)
    monkeypatch.setattr("bhiksha.ops.alerts.shutil.which", lambda name: None)

    command, cwd = _default_lathi_invocation(Path(repo))

    assert command == [sys.executable, "-m", "lathi_bus.cli"]
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


# --- Obsidian coding-agent review surface (status-board #6) ------------------


def _make_report(tmp_path: Path) -> Path:
    source = tmp_path / "trade_session_report_2026-07-09.md"
    source.write_text("# Bhiksha Trade Session - 2026-07-09\n\n- status: `GREEN`\n", encoding="utf-8")
    return source


def test_publish_lathi_review_routes_to_coding_agent_surface(monkeypatch, tmp_path) -> None:
    source = _make_report(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                '{"review_id": "Bhiksha close session report - 2026-07-09", '
                '"note_path": "07 Agents/Coding/Inbox/Bhiksha close session report.md", '
                '"surface": "obsidian"}'
            ),
            stderr="",
        )

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(
        source=source,
        title="Bhiksha close session report - 2026-07-09",
        workspace_root="/repo",
        artifact_id="artifacts/playbook/reports/trade_session_report_2026-07-09.md",
        command=["lathi-bus"],
    )

    assert result.attempted is True
    assert result.ok is True
    assert result.review_id == "Bhiksha close session report - 2026-07-09"
    assert result.note_path == "07 Agents/Coding/Inbox/Bhiksha close session report.md"
    assert result.surface == "obsidian"
    assert result.profile == "coding-agent-northstar"

    args = calls[0]
    # Route: publish subcommand onto the shared coding-agent profile/folder.
    assert args[:2] == ["lathi-bus", "publish"]
    assert args[args.index("--profile") + 1] == "coding-agent-northstar"
    # Artifact: the actual on-disk markdown report is the published source.
    assert args[args.index("--source") + 1] == str(source)
    assert args[args.index("--title") + 1] == "Bhiksha close session report - 2026-07-09"
    assert args[args.index("--owner-consumer") + 1] == "bhiksha"
    assert args[args.index("--workspace-root") + 1] == "/repo"
    assert args[args.index("--artifact-id") + 1] == "artifacts/playbook/reports/trade_session_report_2026-07-09.md"


def test_publish_lathi_review_absolutizes_relative_source(monkeypatch, tmp_path) -> None:
    """The bus CLI runs with cwd switched to the lathi-bus checkout, so a
    caller-relative source (the scheduled job passes the report's repo-relative
    path) must reach the CLI as an absolute path or it resolves against the
    wrong directory there. Regression for the oldmac deploy-verify failure."""
    source = _make_report(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative_source = source.name  # relative to tmp_path cwd
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"review_id": "r", "note_path": "07 Agents/Coding/Inbox/r.md", "surface": "obsidian"}', stderr=""
        )

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(
        source=relative_source,
        title="t",
        command=["lathi-bus"],
        cwd="/somewhere/else",
    )

    assert result.ok is True
    source_arg = calls[0][calls[0].index("--source") + 1]
    assert Path(source_arg).is_absolute()
    assert Path(source_arg) == source.resolve()


def test_publish_lathi_review_honors_profile_env(monkeypatch, tmp_path) -> None:
    source = _make_report(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='{"note_path": "x"}', stderr="")

    monkeypatch.setenv("BHIKSHA_OBSIDIAN_REVIEW_PROFILE", "codex-northstar")
    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(source=source, title="t", command=["lathi-bus"])

    assert result.profile == "codex-northstar"
    assert calls[0][calls[0].index("--profile") + 1] == "codex-northstar"


def test_publish_lathi_review_off_mode_is_noop(monkeypatch, tmp_path) -> None:
    source = _make_report(tmp_path)

    def fake_run(args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("subprocess.run should not run in off mode")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(source=source, title="t", mode="off", command=["lathi-bus"])

    assert result.attempted is False
    assert result.ok is False
    assert result.mode == "off"


def test_publish_lathi_review_missing_source_is_graceful(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("subprocess.run should not run when source is missing")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(
        source=tmp_path / "does-not-exist.md",
        title="t",
        command=["lathi-bus"],
    )

    assert result.attempted is False
    assert result.ok is False
    assert "not found" in (result.error or "")


def test_publish_lathi_review_graceful_when_bus_unreachable(monkeypatch, tmp_path) -> None:
    source = _make_report(tmp_path)

    def fake_run(args, **kwargs):
        raise FileNotFoundError("lathi-bus not installed")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(source=source, title="t", command=["lathi-bus"])

    # No-bus path: attempted, non-ok, error captured, and NO exception raised.
    assert result.attempted is True
    assert result.ok is False
    assert "lathi-bus not installed" in (result.error or "")


def test_publish_lathi_review_nonzero_exit_is_not_ok(monkeypatch, tmp_path) -> None:
    source = _make_report(tmp_path)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 3, stdout="", stderr="vault locked")

    monkeypatch.setattr("bhiksha.ops.alerts.subprocess.run", fake_run)

    result = publish_lathi_review(source=source, title="t", command=["lathi-bus"])

    assert result.attempted is True
    assert result.ok is False
    assert result.return_code == 3
    assert "vault locked" in (result.error or "")
