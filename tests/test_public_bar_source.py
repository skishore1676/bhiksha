from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bhiksha.market_data.adapters.public import PublicBarSource


class StubClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.get_calls: list[str] = []
        self.closed = False

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        self.get_calls.append(endpoint)
        return self.payload

    async def close(self) -> None:
        self.closed = True


def test_public_bar_source_parses_regular_session_bars() -> None:
    client = StubClient(
        {
            "regularMarket": {
                "bars": [
                    {
                        "timestamp": "2026-05-11T13:30:00Z",
                        "open": "101.0",
                        "high": "102.0",
                        "low": "100.5",
                        "close": "101.5",
                        "volume": 123,
                    }
                ]
            },
            "preMarket": {
                "bars": [
                    {
                        "timestamp": "2026-05-11T12:30:00Z",
                        "open": "99.0",
                        "high": "99.5",
                        "low": "98.5",
                        "close": "99.2",
                        "volume": 50,
                    }
                ]
            },
        }
    )
    source = PublicBarSource(client=client)

    bars = asyncio.run(
        source.warm_start(
            "QQQ",
            datetime(2026, 5, 11, 13, 0, tzinfo=UTC),
            datetime(2026, 5, 11, 14, 0, tzinfo=UTC),
        )
    )

    assert len(bars) == 1
    assert bars[0].symbol == "QQQ"
    assert bars[0].timestamp == datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
    assert bars[0].close == 101.5
    assert client.get_calls == [
        "/userapigateway/historicdata/EQUITY/QQQ/DAY/ONE_MINUTE",
    ]


def test_public_latest_completed_bar_excludes_current_minute() -> None:
    client = StubClient(
        {
            "regularMarket": {
                "bars": [
                    {
                        "timestamp": "2026-05-11T13:34:00Z",
                        "open": "100.0",
                        "high": "100.5",
                        "low": "99.5",
                        "close": "100.1",
                        "volume": 10,
                    },
                    {
                        "timestamp": "2026-05-11T13:35:00Z",
                        "open": "100.1",
                        "high": "100.6",
                        "low": "99.8",
                        "close": "100.2",
                        "volume": 11,
                    },
                ]
            }
        }
    )
    source = PublicBarSource(client=client)

    bar = asyncio.run(
        source.fetch_latest_completed_bar(
            "QQQ",
            now=datetime(2026, 5, 11, 13, 35, 22, tzinfo=UTC),
        )
    )

    assert bar is not None
    assert bar.timestamp == datetime(2026, 5, 11, 13, 34, tzinfo=UTC)
