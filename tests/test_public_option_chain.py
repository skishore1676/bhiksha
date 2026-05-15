from __future__ import annotations

from datetime import date

from bhiksha.options.public_chain import PublicOptionChainService


def test_public_option_chain_parse_normalizes_contracts() -> None:
    payload = {
        "baseSymbol": "QQQ",
        "calls": [],
        "puts": [
            {
                "instrument": {"symbol": "QQQ260515P00558000", "type": "OPTION"},
                "bid": "3.06",
                "ask": "3.20",
                "openInterest": 542,
                "optionDetails": {
                    "strikePrice": "558.0",
                    "greeks": {"delta": "-0.373"},
                },
            }
        ],
    }

    contracts = PublicOptionChainService._parse_chain(
        "QQQ",
        payload,
        as_of=date(2026, 5, 14),
        contract_type="ALL",
    )

    assert len(contracts) == 1
    assert contracts[0].option_symbol == "QQQ260515P00558000"
    assert contracts[0].underlying_symbol == "QQQ"
    assert contracts[0].contract_type == "PUT"
    assert contracts[0].expiration_date == "2026-05-15"
    assert contracts[0].dte == 1
    assert contracts[0].strike == 558.0
    assert contracts[0].delta == -0.373
    assert contracts[0].ask == 3.20
    assert contracts[0].open_interest == 542


def test_public_option_chain_parse_respects_contract_type() -> None:
    payload = {
        "baseSymbol": "QQQ",
        "calls": [{"instrument": {"symbol": "QQQ260515C00558000"}, "optionDetails": {"strikePrice": "558"}}],
        "puts": [{"instrument": {"symbol": "QQQ260515P00558000"}, "optionDetails": {"strikePrice": "558"}}],
    }

    contracts = PublicOptionChainService._parse_chain("QQQ", payload, contract_type="CALL")

    assert [contract.contract_type for contract in contracts] == ["CALL"]
