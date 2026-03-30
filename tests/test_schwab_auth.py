from urllib.parse import parse_qs, urlparse

from bhiksha.integrations.schwab.auth import (
    access_token_is_stale,
    build_authorize_url,
    extract_authorization_code,
)
from bhiksha.integrations.schwab.settings import SchwabSettings


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

