from bhiksha.market_data.adapters.polygon import PolygonBarSource


def test_polygon_payload_parsing_returns_bars() -> None:
    payload = {
        "results": [
            {"t": 1711708200000, "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1234},
            {"t": 1711708260000, "o": 100.5, "h": 101.2, "l": 100.2, "c": 101.0, "v": 1500},
        ]
    }

    bars = PolygonBarSource._parse_bars("QQQ", payload)

    assert len(bars) == 2
    assert bars[0].symbol == "QQQ"
    assert bars[1].close == 101.0

