import asyncio
from types import SimpleNamespace

import bhiksha.ops.health as health
from bhiksha.ops.health import check_schwab_setup, check_schwab_token_health
from bhiksha.app.bootstrap import build_runtime


def test_runtime_health_report_contains_configured_providers() -> None:
    runtime = build_runtime()
    report = asyncio.run(runtime.health_report())
    names = {item.name for item in report.provider_health}
    assert names == {"public", "schwab", "schwab_token"}


def test_schwab_setup_health_does_not_expose_authorize_url(monkeypatch) -> None:
    monkeypatch.setenv("SCHWAB_APP_KEY", "app-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "app-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8080")

    ok, detail = asyncio.run(check_schwab_setup())

    assert ok is True
    assert detail == "authorize_url_ready"
    assert "client_id" not in detail
    assert "https://" not in detail


def test_schwab_health_blocks_token_that_will_fail_during_session(monkeypatch) -> None:
    monkeypatch.setattr(
        health,
        "classify_schwab_token_state",
        lambda settings: SimpleNamespace(state="refresh_token_near_expiry"),
    )

    ok, detail = asyncio.run(check_schwab_token_health())

    assert ok is False
    assert detail == "refresh_token_near_expiry"
