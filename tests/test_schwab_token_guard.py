import asyncio
import json
from datetime import UTC, datetime, timedelta

from bhiksha.integrations.schwab.settings import SchwabSettings
import bhiksha.ops.schwab_token_guard as schwab_token_guard
from bhiksha.ops.schwab_token_guard import classify_schwab_token_state, run_schwab_token_guard


def _settings(token_file) -> SchwabSettings:
    return SchwabSettings(app_key="key", app_secret="secret", token_file=str(token_file))


def _write_tokens(token_file, *, access_minutes_old=1, refresh_days_old=1):
    now = datetime.now(UTC)
    token_file.write_text(
        json.dumps(
            {
                "access_token_issued": (now - timedelta(minutes=access_minutes_old)).isoformat(),
                "refresh_token_issued": (now - timedelta(days=refresh_days_old)).isoformat(),
                "token_dictionary": {
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                },
            }
        ),
        encoding="utf-8",
    )


def test_classify_healthy_token(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=1)

    snapshot = classify_schwab_token_state(_settings(token_file))

    assert snapshot.state == "healthy"
    assert snapshot.refresh_token_days_left is not None


def test_guard_refreshes_access_token_when_stale(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=1)
    calls = []

    async def fake_refresh(settings):
        calls.append(settings.token_file)
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)

    result = asyncio.run(run_schwab_token_guard(settings=_settings(token_file), write_receipt=False))

    assert result.ok is True
    assert result.action == "direct_refresh"
    assert result.direct_refresh_attempted is True
    assert result.direct_refresh_ok is True
    assert result.browser.invoked is False
    assert calls == [str(token_file)]


def test_guard_invokes_browser_when_refresh_token_expired_and_auto_enabled(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=8)

    def fake_browser(command):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
        )
    )

    assert result.ok is True
    assert result.initial.state == "refresh_token_expired"
    assert result.action == "refresh_token_expired_browser_renewal"
    assert result.browser.invoked is True
    assert result.final.state == "healthy"


def test_guard_writes_receipt_without_tokens(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=1)
    receipt_dir = tmp_path / "receipts"

    result = asyncio.run(run_schwab_token_guard(settings=_settings(token_file), receipt_dir=receipt_dir))

    assert result.receipt_path is not None
    text = (receipt_dir / "latest.json").read_text(encoding="utf-8")
    assert "access-secret" not in text
    assert "refresh-secret" not in text
    assert "healthy_noop" in text


def test_guard_near_expiry_auto_mode_direct_refresh_and_browser_renewal(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    # refresh_lead_days default is 2.0; refresh token issued 6 days ago is
    # inside the 2-day lead window before the 7-day expiry.
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6)

    refresh_calls = []

    async def fake_refresh(settings):
        refresh_calls.append(settings.token_file)

    browser_calls = []

    def fake_browser(command):
        browser_calls.append(command)
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
        )
    )

    assert result.initial.state == "refresh_token_near_expiry"
    assert result.direct_refresh_attempted is True
    assert result.direct_refresh_ok is True
    assert refresh_calls == [str(token_file)]
    assert result.browser.invoked is True
    assert browser_calls == [["/tmp/fake-refresh"]]
    assert result.action == "refresh_token_near_expiry_browser_renewal"
    assert result.ok is True
    assert result.final.state == "healthy"


def test_guard_near_expiry_successful_renewal_sends_no_alert(tmp_path, monkeypatch) -> None:
    """Operator preference (2026-07-07): a silent successful proactive renewal
    at the near-expiry mark must NOT ping the operator — only a FAILED re-auth
    attempt (or an unusable token) alerts."""
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6)

    async def fake_refresh(settings):
        return None

    def fake_browser(command):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    alerts = []

    def fake_alert(**kwargs):
        alerts.append(kwargs)
        return schwab_token_guard.AlertResult(mode="spool", attempted=True, ok=True)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "send_lathi_alert", fake_alert)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            alert_mode="spool",
            alert_profile="jarvis-northstar",
            write_receipt=False,
        )
    )

    assert result.initial.state == "refresh_token_near_expiry"
    assert result.browser.invoked is True
    assert result.ok is True
    assert alerts == []  # silent success — no operator ping


def test_guard_near_expiry_browser_renewal_fails_stays_usable_and_alerts(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6)

    async def fake_refresh(settings):
        # keep the access token fresh; refresh_days_old stays at 6 so state
        # remains refresh_token_near_expiry on the final classification.
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=6)

    def fake_browser(command):
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=1, stderr_tail="boom")

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    alerts = []

    def fake_alert(**kwargs):
        alerts.append(kwargs)
        return schwab_token_guard.AlertResult(attempted=True, ok=True, mode=kwargs["mode"])

    monkeypatch.setattr(schwab_token_guard, "send_lathi_alert", fake_alert)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            alert_mode="spool",
            alert_profile="jarvis-northstar",
        )
    )

    assert result.direct_refresh_ok is True
    assert result.browser.invoked is True
    assert result.browser.return_code == 1
    # Usable today because direct_refresh kept the access token fresh, even
    # though the proactive browser renewal failed.
    assert result.ok is True
    assert len(alerts) == 1
    assert alerts[0]["profile"] == "jarvis-northstar"
    assert alerts[0]["level"] == "warning"
    assert "FAILED" in alerts[0]["body"]


def test_guard_access_token_stale_auto_mode_never_invokes_browser(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=1)

    async def fake_refresh(settings):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=1)

    browser_calls = []

    def fake_browser(command):
        browser_calls.append(command)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            write_receipt=False,
        )
    )

    assert result.action == "direct_refresh"
    assert result.browser.invoked is False
    assert browser_calls == []
    assert result.ok is True


def test_guard_near_expiry_mode_off_no_browser_renewal(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6)

    async def fake_refresh(settings):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=6)

    browser_calls = []

    def fake_browser(command):
        browser_calls.append(command)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="off",
            write_receipt=False,
        )
    )

    assert result.initial.state == "refresh_token_near_expiry"
    assert result.direct_refresh_attempted is True
    assert result.direct_refresh_ok is True
    assert result.browser.invoked is False
    assert browser_calls == []
    assert result.action == "direct_refresh"
    assert result.ok is True


def test_guard_alerts_when_token_unusable(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "missing_tokens.json"
    alerts = []

    def fake_alert(**kwargs):
        alerts.append(kwargs)
        return schwab_token_guard.AlertResult(attempted=True, ok=True, mode=kwargs["mode"])

    monkeypatch.setattr(schwab_token_guard, "send_lathi_alert", fake_alert)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            write_receipt=False,
            alert_mode="spool",
            alert_profile="jarvis-northstar",
        )
    )

    assert result.ok is False
    assert result.action == "token_file_missing_operator_required"
    assert result.alert.attempted is True
    assert alerts[0]["profile"] == "jarvis-northstar"
    assert "Final state: token_file_missing" in alerts[0]["body"]
