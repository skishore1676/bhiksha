import subprocess

from bhiksha.ops.alerts import send_lathi_alert


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
