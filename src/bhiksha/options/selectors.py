"""Contract selection logic for single-leg option trades."""

from __future__ import annotations

from bhiksha.domain.models import OptionContractSnapshot, OptionSelection, OptionSelectionRequest


class SelectorEmptyError(ValueError):
    """No contract survived the execution-profile filters.

    Carries a per-filter elimination breakdown so the operator can see which
    constraint emptied the candidate set instead of just "no contracts".
    """

    def __init__(self, deployment_id: str, breakdown: dict[str, int]) -> None:
        self.deployment_id = deployment_id
        self.breakdown = breakdown
        detail = ", ".join(f"{name}={count}" for name, count in breakdown.items() if count)
        super().__init__(
            f"No contracts matched the execution profile for {deployment_id} ({detail or 'no candidates supplied'})"
        )


class SingleLegOptionSelector:
    """Select a contract from a snapshot list using execution constraints."""

    def select(
        self,
        request: OptionSelectionRequest,
        contracts: list[OptionContractSnapshot],
    ) -> OptionSelection:
        allowed_type = self._required_contract_type(request)
        dte_min = int(request.execution_params.get("dte_min", 0))
        dte_max = int(request.execution_params.get("dte_max", 7))
        delta_min = request.execution_params.get("target_abs_delta_min")
        delta_max = request.execution_params.get("target_abs_delta_max")
        min_open_interest = int(request.execution_params.get("min_open_interest", 0))
        max_spread_pct = request.execution_params.get("max_bid_ask_spread_pct")

        eliminated = {
            "total_candidates": len(contracts),
            "wrong_underlying": 0,
            "wrong_contract_type": 0,
            "dte_out_of_range": 0,
            "open_interest_below_min": 0,
            "delta_below_min": 0,
            "delta_above_max": 0,
            "spread_above_max": 0,
        }
        filtered: list[OptionContractSnapshot] = []
        for contract in contracts:
            if contract.underlying_symbol != request.symbol:
                eliminated["wrong_underlying"] += 1
                continue
            if contract.contract_type.upper() != allowed_type:
                eliminated["wrong_contract_type"] += 1
                continue
            if not (dte_min <= contract.dte <= dte_max):
                eliminated["dte_out_of_range"] += 1
                continue
            if (contract.open_interest or 0) < min_open_interest:
                eliminated["open_interest_below_min"] += 1
                continue
            if delta_min is not None and (contract.abs_delta is None or contract.abs_delta < float(delta_min)):
                eliminated["delta_below_min"] += 1
                continue
            if delta_max is not None and (contract.abs_delta is None or contract.abs_delta > float(delta_max)):
                eliminated["delta_above_max"] += 1
                continue
            if max_spread_pct is not None and (
                contract.spread_pct is None or contract.spread_pct > float(max_spread_pct)
            ):
                eliminated["spread_above_max"] += 1
                continue
            filtered.append(contract)

        if not filtered:
            raise SelectorEmptyError(request.deployment_id, eliminated)

        target_delta = self._target_delta(delta_min, delta_max)
        ranked = sorted(
            filtered,
            key=lambda contract: (
                abs((contract.abs_delta or 99.0) - target_delta),
                contract.dte,
                contract.spread_pct if contract.spread_pct is not None else 99.0,
            ),
        )
        chosen = ranked[0]
        return OptionSelection(
            option_symbol=chosen.option_symbol,
            contract_type=chosen.contract_type,
            dte=chosen.dte,
            abs_delta=chosen.abs_delta,
            bid=chosen.bid,
            ask=chosen.ask,
            strike=chosen.strike,
        )

    def _required_contract_type(self, request: OptionSelectionRequest) -> str:
        if request.direction.value == "long":
            return str(request.execution_params.get("long_signal_contract_type", "CALL")).upper()
        return str(request.execution_params.get("short_signal_contract_type", "PUT")).upper()

    @staticmethod
    def _target_delta(delta_min: float | None, delta_max: float | None) -> float:
        if delta_min is None and delta_max is None:
            return 0.30
        if delta_min is None:
            return float(delta_max)
        if delta_max is None:
            return float(delta_min)
        return (float(delta_min) + float(delta_max)) / 2
