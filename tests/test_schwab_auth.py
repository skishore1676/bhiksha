from urllib.parse import parse_qs, urlparse

import pytest

from bhiksha.integrations.schwab.auth import (
    access_token_is_stale,
    build_authorize_url,
    extract_authorization_code,
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
