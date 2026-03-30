from bhiksha.integrations.schwab.chain import SchwabOptionChainService


def test_parse_chain_normalizes_symbols_and_quotes() -> None:
    payload = {
        "callExpDateMap": {},
        "putExpDateMap": {
            "2026-03-30:0": {
                "558.0": [
                    {
                        "putCall": "PUT",
                        "symbol": "QQQ   260330P00558000",
                        "delta": -0.373,
                        "bid": 3.06,
                        "ask": 3.20,
                        "openInterest": 542,
                        "strikePrice": 558.0,
                        "daysToExpiration": 0,
                    }
                ]
            }
        },
    }

    contracts = SchwabOptionChainService._parse_chain("QQQ", payload)

    assert len(contracts) == 1
    assert contracts[0].option_symbol == "QQQ260330P00558000"
    assert contracts[0].ask == 3.20

