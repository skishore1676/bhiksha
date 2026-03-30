from datetime import datetime

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest
from bhiksha.options.selectors import SingleLegOptionSelector


def test_single_leg_selector_prefers_contract_near_target_delta() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="market_impulse_qqq_short_v1",
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 3, 30, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={
            "short_signal_contract_type": "PUT",
            "dte_min": 0,
            "dte_max": 7,
            "target_abs_delta_min": 0.20,
            "target_abs_delta_max": 0.40,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.20,
        },
    )

    contracts = [
        OptionContractSnapshot(
            option_symbol="QQQ250330P00100000",
            underlying_symbol="QQQ",
            contract_type="PUT",
            expiration_date="2026-03-30",
            dte=0,
            strike=100.0,
            delta=-0.18,
            bid=1.00,
            ask=1.30,
            open_interest=400,
        ),
        OptionContractSnapshot(
            option_symbol="QQQ250331P00099000",
            underlying_symbol="QQQ",
            contract_type="PUT",
            expiration_date="2026-03-31",
            dte=1,
            strike=99.0,
            delta=-0.29,
            bid=1.10,
            ask=1.18,
            open_interest=500,
        ),
        OptionContractSnapshot(
            option_symbol="QQQ250401P00098000",
            underlying_symbol="QQQ",
            contract_type="PUT",
            expiration_date="2026-04-01",
            dte=2,
            strike=98.0,
            delta=-0.36,
            bid=1.40,
            ask=1.55,
            open_interest=600,
        ),
    ]

    selected = selector.select(request, contracts)

    assert selected.option_symbol == "QQQ250331P00099000"
    assert selected.contract_type == "PUT"

