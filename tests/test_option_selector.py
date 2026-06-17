from datetime import datetime

import pytest

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector


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


def test_single_leg_selector_empty_reports_filter_breakdown() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="amd_short_lane",
        symbol="AMD",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 6, 10, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={
            "short_signal_contract_type": "PUT",
            "dte_min": 3,
            "dte_max": 7,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.20,
        },
    )

    contracts = [
        OptionContractSnapshot(
            option_symbol="AMD260612P00450000",
            underlying_symbol="AMD",
            contract_type="PUT",
            expiration_date="2026-06-12",
            dte=1,
            strike=450.0,
            delta=-0.30,
            bid=2.00,
            ask=2.10,
            open_interest=500,
        ),
        OptionContractSnapshot(
            option_symbol="AMD260617P00450000",
            underlying_symbol="AMD",
            contract_type="PUT",
            expiration_date="2026-06-17",
            dte=5,
            strike=450.0,
            delta=-0.30,
            bid=2.00,
            ask=2.10,
            open_interest=None,
        ),
        OptionContractSnapshot(
            option_symbol="AMD260617P00440000",
            underlying_symbol="AMD",
            contract_type="PUT",
            expiration_date="2026-06-17",
            dte=5,
            strike=440.0,
            delta=-0.25,
            bid=1.00,
            ask=1.40,
            open_interest=300,
        ),
    ]

    with pytest.raises(SelectorEmptyError) as excinfo:
        selector.select(request, contracts)

    breakdown = excinfo.value.breakdown
    assert breakdown["total_candidates"] == 3
    assert breakdown["dte_out_of_range"] == 1
    assert breakdown["open_interest_below_min"] == 1
    assert breakdown["spread_above_max"] == 1
    assert excinfo.value.deployment_id == "amd_short_lane"
    assert "open_interest_below_min=1" in str(excinfo.value)


def test_single_leg_selector_empty_with_no_candidates() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="amd_short_lane",
        symbol="AMD",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 6, 10, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={},
    )

    with pytest.raises(SelectorEmptyError, match="no candidates supplied"):
        selector.select(request, [])


def test_single_leg_selector_allow_nearest_after_uses_next_later_dte_only() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="smh_short_lane",
        symbol="SMH",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 6, 17, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={
            "short_signal_contract_type": "PUT",
            "dte_min": 3,
            "dte_max": 7,
            "dte_fallback_policy": "allow_nearest_after",
            "target_abs_delta_min": 0.15,
            "target_abs_delta_max": 0.35,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.08,
        },
    )

    selected = selector.select(
        request,
        [
            _contract("SMH260618P00610000", dte=1, delta=-0.30, bid=11.45, ask=12.35),
            _contract("SMH260626P00610000", dte=9, delta=-0.30, bid=11.45, ask=12.35),
            _contract("SMH260629P00610000", dte=12, delta=-0.25, bid=9.45, ask=10.05),
        ],
    )

    assert selected.option_symbol == "SMH260626P00610000"
    assert selected.dte == 9
    assert selected.dte_fallback_policy == "allow_nearest_after"
    assert selected.requested_dte_min == 3
    assert selected.requested_dte_max == 7


def test_single_leg_selector_strict_reports_nearest_after_without_selecting_it() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="smh_short_lane",
        symbol="SMH",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 6, 17, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={
            "short_signal_contract_type": "PUT",
            "dte_min": 3,
            "dte_max": 7,
            "target_abs_delta_min": 0.15,
            "target_abs_delta_max": 0.35,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.08,
        },
    )

    with pytest.raises(SelectorEmptyError) as excinfo:
        selector.select(
            request,
            [
                _contract("SMH260618P00610000", dte=1, delta=-0.30, bid=11.45, ask=12.35),
                _contract("SMH260626P00610000", dte=9, delta=-0.30, bid=11.45, ask=12.35),
            ],
        )

    assert excinfo.value.breakdown["dte_out_of_range"] == 2
    assert excinfo.value.diagnostics == {
        "requested_dte_min": 3,
        "requested_dte_max": 7,
        "available_dtes": [1, 9],
        "dte_window_candidates": 0,
        "dte_fallback_policy": "strict",
        "nearest_after_dte": 9,
    }


def test_single_leg_selector_fallback_does_not_rescue_non_dte_filter_failures() -> None:
    selector = SingleLegOptionSelector()
    request = OptionSelectionRequest(
        deployment_id="amd_short_lane",
        symbol="AMD",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 6, 17, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params={
            "short_signal_contract_type": "PUT",
            "dte_min": 3,
            "dte_max": 7,
            "dte_fallback_policy": "allow_nearest_after",
            "target_abs_delta_min": 0.15,
            "target_abs_delta_max": 0.35,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.08,
        },
    )

    with pytest.raises(SelectorEmptyError) as excinfo:
        selector.select(
            request,
            [
                _contract("AMD260622P00150000", symbol="AMD", dte=5, delta=-0.30, bid=2.00, ask=2.50),
                _contract("AMD260626P00150000", symbol="AMD", dte=9, delta=-0.30, bid=2.00, ask=2.05),
            ],
        )

    assert excinfo.value.breakdown["spread_above_max"] == 1
    assert excinfo.value.diagnostics["dte_window_candidates"] == 1


def _contract(
    option_symbol: str,
    *,
    symbol: str = "SMH",
    dte: int,
    delta: float,
    bid: float,
    ask: float,
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        option_symbol=option_symbol,
        underlying_symbol=symbol,
        contract_type="PUT",
        expiration_date="2026-06-26",
        dte=dte,
        strike=610.0,
        delta=delta,
        bid=bid,
        ask=ask,
        open_interest=500,
    )
