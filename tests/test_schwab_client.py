import asyncio
from datetime import UTC, datetime

import pytest

from bhiksha.integrations.schwab.client import SchwabApiClient
from bhiksha.integrations.schwab.market_data_client import (
    SchwabReadOnlyMarketDataClient,
)
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.market_data.adapters.schwab import SchwabBarSource


def test_price_history_omits_period_when_date_bounds_are_supplied() -> None:
    http = RecordingHttpClient()
    client = SchwabApiClient(
        settings=SchwabSettings(api_base_url="https://example.test")
    )
    client._client = http

    async def headers() -> dict[str, str]:
        return {"Authorization": "Bearer token"}

    client._headers = headers
    start = datetime(2026, 3, 30, 14, 30, tzinfo=UTC)
    end = datetime(2026, 3, 30, 14, 31, tzinfo=UTC)

    asyncio.run(
        client.price_history(
            "AMD",
            period_type="day",
            period=None,
            frequency_type="minute",
            frequency=1,
            start_date=start,
            end_date=end,
            need_extended_hours_data=False,
        )
    )

    assert http.calls == [
        {
            "path": "/marketdata/v1/pricehistory",
            "headers": {"Authorization": "Bearer token"},
            "params": {
                "symbol": "AMD",
                "periodType": "day",
                "frequencyType": "minute",
                "frequency": 1,
                "startDate": int(start.timestamp() * 1000),
                "endDate": int(end.timestamp() * 1000),
                "needExtendedHoursData": "false",
            },
        }
    ]


def test_read_only_market_data_client_exposes_no_account_or_order_surface() -> None:
    for prohibited in (
        "linked_accounts",
        "account_details",
        "place_order",
        "replace_order",
        "cancel_order",
        "orders",
    ):
        assert not hasattr(SchwabReadOnlyMarketDataClient, prohibited)


def test_read_only_market_data_settings_never_load_or_capture_trading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHWAB_APP_KEY", "must-not-cross-wire")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "must-not-cross-wire")
    monkeypatch.setenv("SCHWAB_TOKEN_FILE", "/secure/read-only-token.json")

    def prohibited() -> None:
        raise AssertionError("read-only settings attempted to load dotenv")

    monkeypatch.setattr("bhiksha.integrations.schwab.settings.load_dotenv", prohibited)
    settings = SchwabSettings.market_data_from_env()

    assert settings.app_key is None
    assert settings.app_secret is None
    assert settings.token_file == "/secure/read-only-token.json"

    client = type("ReadOnlyClient", (), {"settings": settings})()
    source = SchwabBarSource(client=client)  # type: ignore[arg-type]
    assert source.settings is settings


class RecordingHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get(self, path: str, *, headers: dict, params: dict) -> "StubResponse":
        self.calls.append({"path": path, "headers": headers, "params": params})
        return StubResponse()

    async def aclose(self) -> None:
        return None


class StubResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"candles": []}
