from datetime import UTC, datetime
import asyncio

from bhiksha.integrations.schwab.client import SchwabApiClient
from bhiksha.integrations.schwab.settings import SchwabSettings


def test_price_history_omits_period_when_date_bounds_are_supplied() -> None:
    http = RecordingHttpClient()
    client = SchwabApiClient(settings=SchwabSettings(api_base_url="https://example.test"))
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
