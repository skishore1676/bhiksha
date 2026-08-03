"""Read-only chain-snapshot capture for option-selection attempts.

Every entry attempt fetches the full candidate chain (strike, DTE, OI, delta,
bid/ask) and hands it to ``SingleLegOptionSelector.select`` -- which discards
the per-contract detail once it has picked a winner (or raised
``SelectorEmptyError``), leaving only aggregate rejection counts. That makes
"would a lower OI floor have filled?" unanswerable after the fact.

This module builds a snapshot of the SAME in-memory candidate list at the
SAME instant, purely for persistence/analytics. It mirrors the elimination
cascade in ``selectors.SingleLegOptionSelector.select`` (and its
``_nearest_after_candidates`` fallback) closely enough to label each captured
contract with the reason it would be accepted or rejected -- but it is a
read-only side channel: nothing here feeds back into ``selectors.py``, and a
bug here can never change which contract gets chosen or block an order.
``tests/test_chain_snapshot.py`` includes a cross-check test that runs the
real selector alongside this module and asserts they agree on the winning
contract, as a canary against the two cascades drifting apart.

Volume is bounded deliberately (see ``_capture_candidates``): only contracts
of the correct type (calls for long signals / puts for short) that are inside
the requested DTE window, sit at the single nearest-after-DTE expiry, or share
the expiry actually selected by the fallback are captured. The last clause is
important when the nearest expiry has no liquid contract and the selector
continues to a later expiry; the persisted sidecar must contain its winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING

from bhiksha.domain.models import OptionContractSnapshot, OptionSelectionRequest

if TYPE_CHECKING:
    from bhiksha.domain.models import OptionSelection
    from bhiksha.options.selectors import SelectorEmptyError


@dataclass(slots=True, frozen=True)
class ContractSnapshotRow:
    """One candidate contract's attributes and verdict for one attempt."""

    option_symbol: str
    expiration_date: str
    dte: int
    strike: float
    contract_type: str
    open_interest: int | None
    delta: float | None
    bid: float | None
    ask: float | None
    spread_pct: float | None
    dte_in_window: bool
    verdict: str
    fallback_verdict: str | None
    is_selected: bool


@dataclass(slots=True, frozen=True)
class ChainSnapshotAttempt:
    """One selection attempt: the request context plus its captured rows."""

    snapshot_id: str
    deployment_id: str
    symbol: str
    lane: str
    direction: str
    allowed_contract_type: str
    dte_min: int
    dte_max: int
    min_open_interest: int
    target_abs_delta_min: float | None
    target_abs_delta_max: float | None
    max_bid_ask_spread_pct: float | None
    dte_fallback_policy: str
    nearest_after_dte: int | None
    total_candidates: int
    captured_candidates: int
    selector_empty: bool
    selected_option_symbol: str | None
    option_candidate_set_sha256: str = ""
    actual_option_selection_sha256: str = ""
    rows: list[ContractSnapshotRow] = field(default_factory=list)


# Verdict labels. Mirror the ``eliminated`` breakdown keys in
# ``selectors.SingleLegOptionSelector.select`` so a waterfall built from
# captured rows lines up with the aggregate counts already logged on
# ``SelectorEmptyError``.
VERDICT_ACCEPTED = "accepted"
VERDICT_DTE_OUT_OF_RANGE = "dte_out_of_range"
VERDICT_OI_BELOW_MIN = "open_interest_below_min"
VERDICT_DELTA_BELOW_MIN = "delta_below_min"
VERDICT_DELTA_ABOVE_MAX = "delta_above_max"
VERDICT_SPREAD_ABOVE_MAX = "spread_above_max"


def build_chain_snapshot(
    request: OptionSelectionRequest,
    contracts: list[OptionContractSnapshot],
    *,
    lane: str,
    snapshot_id: str,
    selection: "OptionSelection | None" = None,
    selector_error: "SelectorEmptyError | None" = None,
) -> ChainSnapshotAttempt:
    """Build a bounded, verdict-labeled snapshot of one selection attempt.

    ``selection`` (the winning contract) and ``selector_error`` (raised when
    nothing survived) are mutually exclusive outcomes of the SAME
    ``vehicle_resolver.resolve`` call this snapshot is capturing -- pass
    whichever one the caller actually got.
    """
    allowed_type, dte_min, dte_max, delta_min, delta_max, min_open_interest, max_spread_pct, fallback_policy = (
        _thresholds(request)
    )

    desired = [
        contract
        for contract in contracts
        if contract.underlying_symbol == request.symbol and contract.contract_type.upper() == allowed_type
    ]
    nearest_after_dte = _nearest_after_dte(desired, dte_max=dte_max)
    selected_dte = int(selection.dte) if selection is not None else None
    capture_set = _capture_candidates(
        desired,
        dte_min=dte_min,
        dte_max=dte_max,
        nearest_after_dte=nearest_after_dte,
        selected_dte=selected_dte,
    )

    selected_option_symbol = selection.option_symbol if selection is not None else None
    candidate_rows = [
        _row_for_contract(
            contract,
            dte_min=dte_min,
            dte_max=dte_max,
            delta_min=delta_min,
            delta_max=delta_max,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            selected_option_symbol=selected_option_symbol,
        )
        for contract in desired
    ]
    rows = [
        _row_for_contract(
            contract,
            dte_min=dte_min,
            dte_max=dte_max,
            delta_min=delta_min,
            delta_max=delta_max,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
            selected_option_symbol=selected_option_symbol,
        )
        for contract in capture_set
    ]

    candidate_set_sha256 = _canonical_sha256(
        {
            "schema_version": "bhiksha.option_candidate_set.v1",
            "candidates": [
                _canonical_row_payload(row)
                for row in sorted(candidate_rows, key=lambda item: (item.option_symbol, item.expiration_date, item.strike))
            ],
        }
    )
    selection_sha256 = _canonical_sha256(
        {
            "schema_version": "bhiksha.actual_option_selection.v1",
            "selector_implementation": "bhiksha.options.selectors.SingleLegOptionSelector",
            "selector_version": "1",
            "request": {
                "deployment_id": request.deployment_id,
                "symbol": request.symbol,
                "direction": request.direction.value,
                "allowed_contract_type": allowed_type,
                "dte_min": dte_min,
                "dte_max": dte_max,
                "min_open_interest": min_open_interest,
                "target_abs_delta_min": delta_min,
                "target_abs_delta_max": delta_max,
                "max_bid_ask_spread_pct": max_spread_pct,
                "dte_fallback_policy": fallback_policy,
            },
            "option_candidate_set_sha256": candidate_set_sha256,
            "selected_option_symbol": selected_option_symbol,
            "selector_empty": selector_error is not None,
        }
    )

    return ChainSnapshotAttempt(
        snapshot_id=snapshot_id,
        deployment_id=request.deployment_id,
        symbol=request.symbol,
        lane=lane,
        direction=request.direction.value,
        allowed_contract_type=allowed_type,
        dte_min=dte_min,
        dte_max=dte_max,
        min_open_interest=min_open_interest,
        target_abs_delta_min=delta_min,
        target_abs_delta_max=delta_max,
        max_bid_ask_spread_pct=max_spread_pct,
        dte_fallback_policy=fallback_policy,
        nearest_after_dte=nearest_after_dte,
        total_candidates=len(contracts),
        captured_candidates=len(rows),
        selector_empty=selector_error is not None,
        selected_option_symbol=selected_option_symbol,
        option_candidate_set_sha256=candidate_set_sha256,
        actual_option_selection_sha256=selection_sha256,
        rows=rows,
    )


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_row_payload(row: ContractSnapshotRow) -> dict[str, object]:
    return {
        "option_symbol": row.option_symbol,
        "expiration_date": row.expiration_date,
        "dte": row.dte,
        "strike": row.strike,
        "contract_type": row.contract_type,
        "open_interest": row.open_interest,
        "delta": row.delta,
        "bid": row.bid,
        "ask": row.ask,
        "spread_pct": row.spread_pct,
        "dte_in_window": row.dte_in_window,
        "verdict": row.verdict,
        "fallback_verdict": row.fallback_verdict,
        "is_selected": row.is_selected,
    }


def _thresholds(
    request: OptionSelectionRequest,
) -> tuple[str, int, int, float | None, float | None, int, float | None, str]:
    # Mirrors the parameter reads at the top of
    # selectors.SingleLegOptionSelector.select -- same keys, same defaults.
    allowed_type = _required_contract_type(request)
    dte_min = int(request.execution_params.get("dte_min", 0))
    dte_max = int(request.execution_params.get("dte_max", 7))
    delta_min = request.execution_params.get("target_abs_delta_min")
    delta_max = request.execution_params.get("target_abs_delta_max")
    min_open_interest = int(request.execution_params.get("min_open_interest", 0))
    max_spread_pct = request.execution_params.get("max_bid_ask_spread_pct")
    dte_fallback_policy = str(request.execution_params.get("dte_fallback_policy") or "strict").strip().lower()
    return allowed_type, dte_min, dte_max, delta_min, delta_max, min_open_interest, max_spread_pct, dte_fallback_policy


def _required_contract_type(request: OptionSelectionRequest) -> str:
    # Mirrors selectors.SingleLegOptionSelector._required_contract_type.
    if request.direction.value == "long":
        return str(request.execution_params.get("long_signal_contract_type", "CALL")).upper()
    return str(request.execution_params.get("short_signal_contract_type", "PUT")).upper()


def _nearest_after_dte(desired: list[OptionContractSnapshot], *, dte_max: int) -> int | None:
    # Mirrors selectors.SingleLegOptionSelector._nearest_after_dte.
    later_dtes = sorted({contract.dte for contract in desired if contract.dte > dte_max})
    return later_dtes[0] if later_dtes else None


def _capture_candidates(
    desired: list[OptionContractSnapshot],
    *,
    dte_min: int,
    dte_max: int,
    nearest_after_dte: int | None,
    selected_dte: int | None,
) -> list[OptionContractSnapshot]:
    return [
        contract
        for contract in desired
        if (dte_min <= contract.dte <= dte_max)
        or (nearest_after_dte is not None and contract.dte == nearest_after_dte)
        or (selected_dte is not None and contract.dte == selected_dte)
    ]


def _non_dte_verdict(
    contract: OptionContractSnapshot,
    *,
    delta_min: float | None,
    delta_max: float | None,
    min_open_interest: int,
    max_spread_pct: float | None,
) -> str:
    # Mirrors selectors.SingleLegOptionSelector._passes_non_dte_filters'
    # cascade order (also identical to the tail of select()'s main loop,
    # after its DTE check).
    if (contract.open_interest or 0) < min_open_interest:
        return VERDICT_OI_BELOW_MIN
    if delta_min is not None and (contract.abs_delta is None or contract.abs_delta < float(delta_min)):
        return VERDICT_DELTA_BELOW_MIN
    if delta_max is not None and (contract.abs_delta is None or contract.abs_delta > float(delta_max)):
        return VERDICT_DELTA_ABOVE_MAX
    if max_spread_pct is not None and (
        contract.spread_pct is None or contract.spread_pct > float(max_spread_pct)
    ):
        return VERDICT_SPREAD_ABOVE_MAX
    return VERDICT_ACCEPTED


def _row_for_contract(
    contract: OptionContractSnapshot,
    *,
    dte_min: int,
    dte_max: int,
    delta_min: float | None,
    delta_max: float | None,
    min_open_interest: int,
    max_spread_pct: float | None,
    selected_option_symbol: str | None,
) -> ContractSnapshotRow:
    dte_in_window = dte_min <= contract.dte <= dte_max
    if dte_in_window:
        verdict = _non_dte_verdict(
            contract,
            delta_min=delta_min,
            delta_max=delta_max,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
        )
        fallback_verdict = None
    else:
        verdict = VERDICT_DTE_OUT_OF_RANGE
        fallback_verdict = _non_dte_verdict(
            contract,
            delta_min=delta_min,
            delta_max=delta_max,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
        )

    return ContractSnapshotRow(
        option_symbol=contract.option_symbol,
        expiration_date=contract.expiration_date,
        dte=contract.dte,
        strike=contract.strike,
        contract_type=contract.contract_type,
        open_interest=contract.open_interest,
        delta=contract.delta,
        bid=contract.bid,
        ask=contract.ask,
        spread_pct=contract.spread_pct,
        dte_in_window=dte_in_window,
        verdict=verdict,
        fallback_verdict=fallback_verdict,
        is_selected=selected_option_symbol is not None and contract.option_symbol == selected_option_symbol,
    )
