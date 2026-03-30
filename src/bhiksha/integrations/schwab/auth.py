"""Manual-first Schwab OAuth helpers."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens, write_tokens


def build_authorize_url(settings: SchwabSettings) -> str:
    """Build the Schwab OAuth authorization URL."""
    settings.validate_credentials()
    return f"{settings.authorize_url}?{urlencode({'client_id': settings.app_key, 'redirect_uri': settings.callback_url})}"


def extract_authorization_code(returned_url: str) -> str:
    """Extract the authorization code from the returned callback URL."""
    parsed = urlparse(returned_url.strip())
    values = parse_qs(parsed.query)
    code = values.get("code", [None])[0]
    if not code:
        raise ValueError("Authorization code not found in callback URL")
    return code


def _basic_auth_header(settings: SchwabSettings) -> str:
    settings.validate_credentials()
    raw = f"{settings.app_key}:{settings.app_secret}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('utf-8')}"


def _normalize_token_payload(token_dictionary: dict[str, Any], refresh_issued_at: datetime | None = None) -> dict[str, Any]:
    access_issued_at = datetime.now(UTC)
    refresh_issued_at = refresh_issued_at or access_issued_at
    return {
        "access_token_issued": access_issued_at.isoformat(),
        "refresh_token_issued": refresh_issued_at.isoformat(),
        "token_dictionary": token_dictionary,
    }


async def exchange_code_for_tokens(returned_url: str, settings: SchwabSettings) -> dict[str, Any]:
    """Exchange a callback URL for fresh access and refresh tokens."""
    code = extract_authorization_code(returned_url)
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        response = await client.post(
            settings.token_url,
            headers={
                "Authorization": _basic_auth_header(settings),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.callback_url,
            },
        )
        response.raise_for_status()
        payload = _normalize_token_payload(response.json())
        write_tokens(settings.token_file, payload)
        return payload


async def refresh_access_token(settings: SchwabSettings) -> dict[str, Any]:
    """Refresh the access token using the stored refresh token."""
    existing = read_tokens(settings.token_file)
    if not existing:
        raise ValueError("No Schwab token file found; complete manual auth first")
    token_dictionary = existing.get("token_dictionary", {})
    refresh_token = token_dictionary.get("refresh_token")
    if not refresh_token:
        raise ValueError("No Schwab refresh token found; complete manual auth first")

    refresh_issued = datetime.fromisoformat(existing["refresh_token_issued"])
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        response = await client.post(
            settings.token_url,
            headers={
                "Authorization": _basic_auth_header(settings),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        response.raise_for_status()
        payload = _normalize_token_payload(response.json(), refresh_issued_at=refresh_issued)
        write_tokens(settings.token_file, payload)
        return payload


def access_token_is_stale(payload: dict[str, Any], buffer_seconds: int = 60) -> bool:
    """Return True when the access token should be refreshed."""
    issued_at = datetime.fromisoformat(payload["access_token_issued"])
    return datetime.now(UTC) >= issued_at + timedelta(seconds=1800 - buffer_seconds)


def refresh_token_is_expired(payload: dict[str, Any], buffer_seconds: int = 1800) -> bool:
    """Return True when the refresh token can no longer be trusted."""
    issued_at = datetime.fromisoformat(payload["refresh_token_issued"])
    return datetime.now(UTC) >= issued_at + timedelta(days=7) - timedelta(seconds=buffer_seconds)


async def get_valid_access_token(settings: SchwabSettings) -> str:
    """Load or refresh the Schwab access token."""
    payload = read_tokens(settings.token_file)
    if not payload:
        raise ValueError("No Schwab token file found; run manual auth first")
    if refresh_token_is_expired(payload):
        raise ValueError("Schwab refresh token has expired; run manual auth again")
    if access_token_is_stale(payload):
        payload = await refresh_access_token(settings)
    token_dictionary = payload.get("token_dictionary", {})
    access_token = token_dictionary.get("access_token")
    if not access_token:
        raise ValueError("Schwab access token missing from token payload")
    return str(access_token)

