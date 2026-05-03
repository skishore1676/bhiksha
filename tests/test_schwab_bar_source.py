from datetime import UTC, datetime, timedelta

import asyncio

from bhiksha.market_data.adapters.schwab import SchwabBarSource


def test_latest_completed_bar_ignores_current_open_minute() -> None:
    now = datetime(2026, 3, 30, 14, 35, 20, tzinfo=UTC)
    candles = [
        {
            "datetime": int(datetime(2026, 3, 30, 14, 34, 0, tzinfo=UTC).timestamp() * 1000),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1000,
        },
        {
            "datetime": int(datetime(2026, 3, 30, 14, 35, 0, tzinfo=UTC).timestamp() * 1000),
            "open": 100.5,
            "high": 101.5,
            "low": 100,
            "close": 101.0,
            "volume": 1200,
        },
    ]

    bar = SchwabBarSource._latest_completed_bar("QQQ", candles, now=now)

    assert bar is not None
    assert bar.timestamp.isoformat().startswith("2026-03-30T14:34:00")


def test_warm_start_uses_requested_range_and_regular_hours() -> None:
    class StubClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def price_history(self, symbol: str, **kwargs) -> dict:
            self.calls.append({"symbol": symbol, **kwargs})
            before = datetime(2026, 3, 30, 14, 29, tzinfo=UTC)
            inside = datetime(2026, 3, 30, 14, 30, tzinfo=UTC)
            after = datetime(2026, 3, 30, 14, 32, tzinfo=UTC)
            return {
                "candles": [
                    _candle(before, close=99.0),
                    _candle(inside, close=100.0),
                    _candle(after, close=101.0),
                ]
            }

    client = StubClient()
    source = SchwabBarSource(client=client)
    start = datetime(2026, 3, 30, 14, 30, tzinfo=UTC)
    end = start + timedelta(minutes=1)

    bars = asyncio.run(source.warm_start("AMD", start, end))

    assert len(bars) == 1
    assert bars[0].timestamp == start
    assert client.calls == [
        {
            "symbol": "AMD",
            "period_type": "day",
            "period": None,
            "frequency_type": "minute",
            "frequency": 1,
            "start_date": start,
            "end_date": end,
            "need_extended_hours_data": False,
        }
    ]


def test_fetch_live_price_prefers_mark_and_quote_time() -> None:
    class StubClient:
        async def quote(self, symbol: str) -> dict:
            return {
                symbol: {
                    "quote": {
                        "mark": 259.62,
                        "lastPrice": 259.61,
                        "quoteTime": 1775841546669,
                    },
                    "regular": {
                        "regularMarketLastPrice": 259.6,
                    },
                }
            }

    source = SchwabBarSource(client=StubClient())

    price, timestamp = asyncio.run(source.fetch_live_price("AAPL"))

    assert price == 259.62
    assert timestamp == datetime.fromtimestamp(1775841546669 / 1000, tz=UTC)


def _candle(timestamp: datetime, *, close: float) -> dict:
    return {
        "datetime": int(timestamp.timestamp() * 1000),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
    }
