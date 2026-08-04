import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from bhiksha.integrations.schwab.auth import (
    access_token_is_stale,
    build_authorize_url,
    extract_authorization_code,
    get_read_only_access_token,
    refresh_access_token,
)
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import SchwabTokenStoreError, read_tokens


def test_build_authorize_url_uses_configured_callback() -> None:
    settings = SchwabSettings(
        app_key="a" * 32,
        app_secret="b" * 16,
        callback_url="https://127.0.0.1:8080",
    )

    url = build_authorize_url(settings)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert query["client_id"][0] == "a" * 32
    assert query["redirect_uri"][0] == "https://127.0.0.1:8080"


def test_extract_authorization_code_from_callback_url() -> None:
    returned = "https://127.0.0.1:8080?code=abc123%40&session=xyz"
    assert extract_authorization_code(returned) == "abc123@"


def test_access_token_staleness_detection() -> None:
    payload = {
        "access_token_issued": "2026-03-29T00:00:00+00:00",
        "refresh_token_issued": "2026-03-29T00:00:00+00:00",
        "token_dictionary": {"access_token": "token"},
    }
    assert access_token_is_stale(payload, buffer_seconds=1800) is True


def test_read_tokens_raises_for_invalid_existing_token_file(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    token_file.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SchwabTokenStoreError, match="Invalid JSON"):
        read_tokens(token_file)


def test_read_tokens_returns_none_for_missing_token_file(tmp_path) -> None:
    assert read_tokens(tmp_path / "missing_tokens.json") is None


def test_read_only_access_token_never_refreshes_or_persists(
    tmp_path, monkeypatch
) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    now = datetime.now(UTC)
    token_file.write_text(
        json.dumps(
            {
                "access_token_issued": (now - timedelta(minutes=31)).isoformat(),
                "refresh_token_issued": now.isoformat(),
                "token_dictionary": {
                    "access_token": "stale-access",
                    "refresh_token": "valid-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    original = token_file.read_bytes()

    def prohibited(*args, **kwargs):
        raise AssertionError("read-only token path attempted mutation")

    monkeypatch.setattr(
        "bhiksha.integrations.schwab.auth.refresh_access_token", prohibited
    )
    monkeypatch.setattr("bhiksha.integrations.schwab.auth.write_tokens", prohibited)
    with pytest.raises(ValueError, match="cannot refresh"):
        get_read_only_access_token(SchwabSettings(token_file=str(token_file)))
    assert token_file.read_bytes() == original


def test_refresh_access_token_preserves_existing_refresh_token_when_omitted(
    tmp_path, monkeypatch
) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    issued_at = "2026-04-10T14:30:00+00:00"
    token_file.write_text(
        json.dumps(
            {
                "access_token_issued": issued_at,
                "refresh_token_issued": issued_at,
                "token_dictionary": {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "bhiksha.integrations.schwab.auth.httpx.AsyncClient",
        _fake_client({"access_token": "new-access"}),
    )
    settings = SchwabSettings(
        app_key="key", app_secret="secret", token_file=str(token_file)
    )

    payload = asyncio.run(refresh_access_token(settings))

    assert payload["token_dictionary"]["access_token"] == "new-access"
    assert payload["token_dictionary"]["refresh_token"] == "old-refresh"
    assert payload["refresh_token_issued"] == issued_at


def test_refresh_access_token_rolls_refresh_issue_time_when_schwab_returns_new_refresh_token(
    tmp_path, monkeypatch
) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    issued_at = "2026-04-10T14:30:00+00:00"
    token_file.write_text(
        json.dumps(
            {
                "access_token_issued": issued_at,
                "refresh_token_issued": issued_at,
                "token_dictionary": {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "bhiksha.integrations.schwab.auth.httpx.AsyncClient",
        _fake_client({"access_token": "new-access", "refresh_token": "new-refresh"}),
    )
    settings = SchwabSettings(
        app_key="key", app_secret="secret", token_file=str(token_file)
    )

    payload = asyncio.run(refresh_access_token(settings))

    assert payload["token_dictionary"]["refresh_token"] == "new-refresh"
    assert datetime.fromisoformat(
        payload["refresh_token_issued"]
    ) > datetime.fromisoformat(issued_at)
    assert datetime.fromisoformat(payload["refresh_token_issued"]).tzinfo is not None


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return dict(self._payload)


def _fake_client(payload: dict):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse(payload)

    return FakeAsyncClient
