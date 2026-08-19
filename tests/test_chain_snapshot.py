from datetime import datetime

import pytest

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest
from bhiksha.options.chain_snapshot import (
    VERDICT_ACCEPTED,
    VERDICT_DELTA_ABOVE_MAX,
    VERDICT_DTE_OUT_OF_RANGE,
    VERDICT_OI_BELOW_MIN,
    VERDICT_SPREAD_ABOVE_MAX,
    build_chain_snapshot,
)
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector


def _request(**execution_params_overrides) -> OptionSelectionRequest:
    execution_params = {
        "short_signal_contract_type": "PUT",
        "long_signal_contract_type": "CALL",
        "dte_min": 0,
        "dte_max": 1,
        "target_abs_delta_min": 0.15,
        "target_abs_delta_max": 0.35,
        "min_open_interest": 100,
        "max_bid_ask_spread_pct": 0.10,
    }
    execution_params.update(execution_params_overrides)
    return OptionSelectionRequest(
        deployment_id="smh_short_lane",
        symbol="SMH",
        direction=SignalDirection.SHORT,
        signal_timestamp=datetime(2026, 7, 7, 14, 30, 0),
        execution_profile="single_leg_long_premium_v1",
        execution_params=execution_params,
    )


def _contract(
    option_symbol: str,
    *,
    symbol: str = "SMH",
    contract_type: str = "PUT",
    dte: int = 0,
    strike: float = 250.0,
    delta: float = -0.25,
    bid: float = 3.00,
    ask: float = 3.10,
    open_interest: int | None = 500,
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        option_symbol=option_symbol,
        underlying_symbol=symbol,
        contract_type=contract_type,
        expiration_date="2026-07-08",
        dte=dte,
        strike=strike,
        delta=delta,
        bid=bid,
        ask=ask,
        open_interest=open_interest,
    )


def test_build_chain_snapshot_labels_accepted_and_selected_contract() -> None:
    request = _request()
    contracts = [
        _contract("SMH260708P00250000", dte=0, delta=-0.25, open_interest=500),
    ]

    attempt = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="snap-1",
        selection=SingleLegOptionSelector().select(request, contracts),
    )

    assert attempt.captured_candidates == 1
    assert attempt.selector_empty is False
    assert attempt.selected_option_symbol == "SMH260708P00250000"
    assert len(attempt.option_candidate_set_sha256) == 64
    assert len(attempt.actual_option_selection_sha256) == 64
    row = attempt.rows[0]
    assert row.verdict == VERDICT_ACCEPTED
    assert row.is_selected is True
    assert row.dte_in_window is True
    assert row.fallback_verdict is None

    repeated = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="different-attempt-id",
        selection=SingleLegOptionSelector().select(request, contracts),
    )
    assert repeated.option_candidate_set_sha256 == attempt.option_candidate_set_sha256
    assert repeated.actual_option_selection_sha256 == attempt.actual_option_selection_sha256


def test_build_chain_snapshot_labels_each_filter_rejection() -> None:
    request = _request()
    contracts = [
        _contract("SMH_OI_LOW", open_interest=10),
        _contract("SMH_DELTA_HIGH", delta=-0.60, open_interest=500),
        _contract("SMH_SPREAD_WIDE", bid=1.00, ask=2.00, open_interest=500),
        _contract("SMH_OUT_OF_WINDOW", dte=9, open_interest=500),
    ]

    with pytest.raises(SelectorEmptyError):
        SingleLegOptionSelector().select(request, contracts)

    attempt = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="snap-2",
        selector_error=SelectorEmptyError("smh_short_lane", {}),
    )

    verdicts = {row.option_symbol: row.verdict for row in attempt.rows}
    assert verdicts["SMH_OI_LOW"] == VERDICT_OI_BELOW_MIN
    assert verdicts["SMH_DELTA_HIGH"] == VERDICT_DELTA_ABOVE_MAX
    assert verdicts["SMH_SPREAD_WIDE"] == VERDICT_SPREAD_ABOVE_MAX
    # SMH_OUT_OF_WINDOW's dte (9) is > dte_max (1) so it is the nearest-after
    # candidate: captured, primary verdict is dte_out_of_range, and it gets a
    # SEPARATE fallback_verdict computed on the non-DTE cascade only.
    assert verdicts["SMH_OUT_OF_WINDOW"] == VERDICT_DTE_OUT_OF_RANGE
    fallback_row = next(row for row in attempt.rows if row.option_symbol == "SMH_OUT_OF_WINDOW")
    assert fallback_row.fallback_verdict == VERDICT_ACCEPTED
    assert attempt.selector_empty is True
    assert attempt.selected_option_symbol is None
    assert attempt.nearest_after_dte == 9


def test_build_chain_snapshot_excludes_wrong_type_and_far_out_of_window_contracts() -> None:
    request = _request()
    contracts = [
        _contract("SMH_PUT_IN_WINDOW", contract_type="PUT", dte=0),
        _contract("SMH_CALL_WRONG_TYPE", contract_type="CALL", dte=0),
        _contract("SMH_PUT_NEAREST_AFTER", contract_type="PUT", dte=5),
        _contract("SMH_PUT_FAR_AFTER", contract_type="PUT", dte=30),
        _contract("QQQ_WRONG_UNDERLYING", symbol="QQQ", contract_type="PUT", dte=0),
    ]

    attempt = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="snap-3",
        selector_error=SelectorEmptyError("smh_short_lane", {}),
    )

    captured_symbols = {row.option_symbol for row in attempt.rows}
    # Correct type + in-window, and correct type + the single nearest-after
    # expiry are captured; the wrong-type call, the wrong-underlying put, and
    # the FAR (non-nearest) out-of-window put are all excluded -- this is the
    # bounding that keeps a ~1,122-contract chain down to ~300 rows.
    assert captured_symbols == {"SMH_PUT_IN_WINDOW", "SMH_PUT_NEAREST_AFTER"}
    assert attempt.total_candidates == len(contracts)
    assert attempt.nearest_after_dte == 5


def test_build_chain_snapshot_cross_check_against_real_selector_winner() -> None:
    """Canary against the capture cascade drifting from selectors.py's real one."""
    request = _request(dte_min=3, dte_max=7, dte_fallback_policy="allow_nearest_after")
    contracts = [
        _contract("SMH260709P00610000", dte=1, delta=-0.30),
        _contract("SMH260717P00610000", dte=9, delta=-0.30, bid=11.45, ask=12.35),
        _contract("SMH260720P00610000", dte=12, delta=-0.25),
    ]

    real_selection = SingleLegOptionSelector().select(request, contracts)

    attempt = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="snap-4",
        selection=real_selection,
    )

    selected_rows = [row for row in attempt.rows if row.is_selected]
    assert len(selected_rows) == 1
    assert selected_rows[0].option_symbol == real_selection.option_symbol
    assert real_selection.option_symbol == "SMH260717P00610000"


def test_build_chain_snapshot_records_only_the_bounded_nearest_fallback() -> None:
    request = _request(
        dte_min=0,
        dte_max=3,
        dte_fallback_policy="allow_nearest_after",
    )
    contracts = [
        _contract(
            "SMH_NEAREST_ILLIQUID",
            dte=4,
            bid=1.00,
            ask=2.00,
        ),
        _contract(
            "SMH_FARTHER_LIQUID",
            dte=11,
            bid=3.00,
            ask=3.10,
        ),
    ]

    with pytest.raises(SelectorEmptyError) as excinfo:
        SingleLegOptionSelector().select(request, contracts)
    attempt = build_chain_snapshot(
        request,
        contracts,
        lane="live",
        snapshot_id="snap-farther-fallback",
        selector_error=excinfo.value,
    )

    assert attempt.nearest_after_dte == 4
    assert {row.dte for row in attempt.rows} == {4}
    assert [row for row in attempt.rows if row.is_selected] == []


def test_build_chain_snapshot_handles_empty_chain() -> None:
    request = _request()
    attempt = build_chain_snapshot(
        request,
        [],
        lane="shadow",
        snapshot_id="snap-5",
        selector_error=SelectorEmptyError("smh_short_lane", {}),
    )
    assert attempt.rows == []
    assert attempt.total_candidates == 0
    assert attempt.captured_candidates == 0
    assert attempt.lane == "shadow"
