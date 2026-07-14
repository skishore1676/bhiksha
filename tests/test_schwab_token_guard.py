import asyncio
import json
from datetime import UTC, datetime, timedelta
import subprocess

from bhiksha.integrations.schwab.settings import SchwabSettings
import bhiksha.ops.schwab_token_guard as schwab_token_guard
from bhiksha.ops.schwab_health import SchwabHealthResult, SymbolHealth
from bhiksha.ops.schwab_token_guard import classify_schwab_token_state, run_schwab_token_guard


def _settings(token_file) -> SchwabSettings:
    return SchwabSettings(app_key="key", app_secret="secret", token_file=str(token_file))


TUESDAY_PREMARKET = datetime(2026, 7, 14, 12, 10, tzinfo=UTC)
THURSDAY_AFTER_CLOSE = datetime(2026, 7, 16, 20, 20, tzinfo=UTC)
FRIDAY_AFTER_CLOSE = datetime(2026, 7, 17, 20, 20, tzinfo=UTC)


async def _healthy_schwab_healthcheck(**kwargs):
    return SchwabHealthResult(
        ok=True,
        linked_account_count=1,
        symbols=[
            SymbolHealth(symbol="QQQ", quote_ok=True, chain_ok=True, call_expirations=1, put_expirations=1),
            SymbolHealth(symbol="IWM", quote_ok=True, chain_ok=True, call_expirations=1, put_expirations=1),
        ],
    )


def _write_tokens(token_file, *, access_minutes_old=1, refresh_days_old=1, now=None):
    now = now or datetime.now(UTC)
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


def test_after_close_guard_renews_before_friday_morning_expiry(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    snapshot = classify_schwab_token_state(_settings(token_file), now=THURSDAY_AFTER_CLOSE)

    assert snapshot.state == "refresh_token_near_expiry"
    assert snapshot.next_trading_session == "2026-07-17"
    assert snapshot.refresh_token_survives_next_session is False


def test_friday_after_close_guard_renews_before_monday_morning_expiry(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, refresh_days_old=4.5, now=FRIDAY_AFTER_CLOSE)

    snapshot = classify_schwab_token_state(_settings(token_file), now=FRIDAY_AFTER_CLOSE)

    assert snapshot.state == "refresh_token_near_expiry"
    assert snapshot.next_trading_session == "2026-07-20"


def test_access_staleness_cannot_mask_unsafe_refresh_token(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    snapshot = classify_schwab_token_state(_settings(token_file), now=THURSDAY_AFTER_CLOSE)

    assert snapshot.state == "refresh_token_near_expiry"


def test_session_survival_includes_the_thirty_minute_refresh_buffer(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    friday_premarket = datetime(2026, 7, 17, 12, 10, tzinfo=UTC)
    raw_expiry = datetime(2026, 7, 17, 20, 30, tzinfo=UTC)
    refresh_issued = raw_expiry - timedelta(days=7)
    _write_tokens(token_file, refresh_days_old=(friday_premarket - refresh_issued).total_seconds() / 86400, now=friday_premarket)

    snapshot = classify_schwab_token_state(_settings(token_file), now=friday_premarket)

    assert snapshot.refresh_token_expires_at == raw_expiry.isoformat()
    assert snapshot.refresh_token_trusted_until == datetime(2026, 7, 17, 20, 0, tzinfo=UTC).isoformat()
    assert snapshot.refresh_token_survives_next_session is False
    assert snapshot.state == "refresh_token_near_expiry"


def test_guard_refreshes_access_token_when_stale(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=1, now=TUESDAY_PREMARKET)
    calls = []

    async def fake_refresh(settings):
        calls.append(settings.token_file)
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=TUESDAY_PREMARKET)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)

    result = asyncio.run(run_schwab_token_guard(settings=_settings(token_file), write_receipt=False, now=TUESDAY_PREMARKET))

    assert result.ok is True
    assert result.action == "direct_refresh"
    assert result.direct_refresh_attempted is True
    assert result.direct_refresh_ok is True
    assert result.browser.invoked is False
    assert calls == [str(token_file)]


def test_guard_invokes_browser_when_refresh_token_expired_and_auto_enabled(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=8, now=TUESDAY_PREMARKET)

    def fake_browser(command, *, force=False):
        assert force is True
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=TUESDAY_PREMARKET)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    proof_token_files = []

    async def fake_healthcheck(*, settings):
        proof_token_files.append(settings.token_file)
        return await _healthy_schwab_healthcheck()

    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "run_schwab_healthcheck", fake_healthcheck)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.ok is True
    assert result.initial.state == "refresh_token_expired"
    assert result.action == "refresh_token_expired_browser_renewal"
    assert result.browser.invoked is True
    assert result.final.state == "healthy"
    assert proof_token_files == [str(token_file)]


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
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    refresh_calls = []

    async def fake_refresh(settings):
        refresh_calls.append(settings.token_file)

    browser_calls = []

    def fake_browser(command, *, force=False):
        assert force is True
        browser_calls.append(command)
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=THURSDAY_AFTER_CLOSE)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "run_schwab_healthcheck", _healthy_schwab_healthcheck)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=THURSDAY_AFTER_CLOSE,
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
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    async def fake_refresh(settings):
        return None

    def fake_browser(command, *, force=False):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=THURSDAY_AFTER_CLOSE)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    alerts = []

    def fake_alert(**kwargs):
        alerts.append(kwargs)
        return schwab_token_guard.AlertResult(mode="spool", attempted=True, ok=True)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "run_schwab_healthcheck", _healthy_schwab_healthcheck)
    monkeypatch.setattr(schwab_token_guard, "send_lathi_alert", fake_alert)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            alert_mode="spool",
            alert_profile="jarvis-northstar",
            write_receipt=False,
            now=THURSDAY_AFTER_CLOSE,
        )
    )

    assert result.initial.state == "refresh_token_near_expiry"
    assert result.browser.invoked is True
    assert result.ok is True
    assert alerts == []  # silent success — no operator ping


def test_guard_rejects_browser_success_without_new_refresh_token(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    async def fake_refresh(settings):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    def fake_browser(command, *, force=False):
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=THURSDAY_AFTER_CLOSE,
        )
    )

    assert result.ok is False
    assert result.failure_kind == "browser_renewal_failed"


def test_forced_browser_renewal_without_configured_worker_fails_closed(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=1, now=TUESDAY_PREMARKET)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="force",
            browser_renewal_cmd=[],
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.browser.invoked is False
    assert result.ok is False
    assert result.attention_required is True
    assert result.failure_kind == "browser_renewal_failed"


def test_browser_worker_timeout_kills_process_group_and_becomes_guard_failure(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=8, now=TUESDAY_PREMARKET)

    class FakeProcess:
        pid = 4242
        returncode = -15
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["/tmp/fake-refresh"], 900, stderr="worker exceeded deadline")
            return "", "worker stopped"

    process = FakeProcess()
    signals = []
    monkeypatch.setattr(schwab_token_guard.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(schwab_token_guard.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.browser.invoked is True
    assert result.browser.return_code == 124
    assert "timed out" in result.browser.stderr_tail
    assert signals == [(4242, schwab_token_guard.signal.SIGTERM)]
    assert result.ok is False
    assert result.failure_kind == "browser_renewal_failed"


def test_guard_persists_failed_post_renewal_health_proof(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=8, now=TUESDAY_PREMARKET)

    def fake_browser(command, *, force=False):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=TUESDAY_PREMARKET)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    async def failed_healthcheck(**kwargs):
        return SchwabHealthResult(ok=False, linked_account_count=1, error="market_data_unavailable")

    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "run_schwab_healthcheck", failed_healthcheck)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.health is not None
    assert result.health.ok is False
    assert result.ok is False
    assert result.failure_kind == "browser_renewal_failed"


def test_post_renewal_health_proof_has_a_bounded_timeout(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=8, now=TUESDAY_PREMARKET)

    def fake_browser(command, *, force=False):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=0, now=TUESDAY_PREMARKET)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    async def slow_healthcheck(**kwargs):
        await asyncio.sleep(0.05)
        return await _healthy_schwab_healthcheck()

    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)
    monkeypatch.setattr(schwab_token_guard, "run_schwab_healthcheck", slow_healthcheck)
    monkeypatch.setenv("BHIKSHA_SCHWAB_POST_RENEWAL_HEALTH_TIMEOUT_SECONDS", "0.001")

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            browser_renewal_cmd=["/tmp/fake-refresh"],
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.health is not None
    assert result.health.error == "healthcheck_timeout"
    assert result.ok is False


def test_browser_output_redaction_covers_oauth_state_basic_auth_and_secrets() -> None:
    raw = (
        "https://localhost/callback?code=abc&state=xyz&sessionId=s1 "
        "Authorization: Basic dXNlcjpwYXNz app_secret=hush client_secret=quiet"
    )

    redacted = schwab_token_guard._redact(raw)

    for secret in ("abc", "xyz", "s1", "dXNlcjpwYXNz", "hush", "quiet"):
        assert secret not in redacted


def test_guard_near_expiry_browser_renewal_failure_blocks_session_and_alerts(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    async def fake_refresh(settings):
        # keep the access token fresh; refresh_days_old stays at 6 so state
        # remains refresh_token_near_expiry on the final classification.
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    def fake_browser(command, *, force=False):
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
            now=THURSDAY_AFTER_CLOSE,
        )
    )

    assert result.direct_refresh_ok is True
    assert result.browser.invoked is True
    assert result.browser.return_code == 1
    assert result.ok is False
    assert result.attention_required is True
    assert result.failure_kind == "browser_renewal_failed"
    assert len(alerts) == 1
    assert alerts[0]["profile"] == "jarvis-northstar"
    assert alerts[0]["level"] == "error"
    assert "FAILED" in alerts[0]["body"]


def test_guard_access_token_stale_auto_mode_never_invokes_browser(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=40, refresh_days_old=1, now=TUESDAY_PREMARKET)

    async def fake_refresh(settings):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=1, now=TUESDAY_PREMARKET)

    browser_calls = []

    def fake_browser(command, *, force=False):
        browser_calls.append(command)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="auto",
            write_receipt=False,
            now=TUESDAY_PREMARKET,
        )
    )

    assert result.action == "direct_refresh"
    assert result.browser.invoked is False
    assert browser_calls == []
    assert result.ok is True


def test_guard_near_expiry_mode_off_no_browser_renewal(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    _write_tokens(token_file, access_minutes_old=1, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    async def fake_refresh(settings):
        _write_tokens(token_file, access_minutes_old=0, refresh_days_old=6.5, now=THURSDAY_AFTER_CLOSE)

    browser_calls = []

    def fake_browser(command, *, force=False):
        browser_calls.append(command)
        return schwab_token_guard.BrowserRenewalResult(invoked=True, command=command or [], return_code=0)

    monkeypatch.setattr(schwab_token_guard.schwab_auth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(schwab_token_guard, "_invoke_browser_renewal", fake_browser)

    result = asyncio.run(
        run_schwab_token_guard(
            settings=_settings(token_file),
            browser_renewal_mode="off",
            write_receipt=False,
            now=THURSDAY_AFTER_CLOSE,
        )
    )

    assert result.initial.state == "refresh_token_near_expiry"
    assert result.direct_refresh_attempted is True
    assert result.direct_refresh_ok is True
    assert result.browser.invoked is False
    assert browser_calls == []
    assert result.action == "direct_refresh"
    assert result.ok is False
    assert result.attention_required is True


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
