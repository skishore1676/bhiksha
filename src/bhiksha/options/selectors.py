"""Contract selection logic for single-leg option trades."""

from __future__ import annotations

from bhiksha.domain.models import OptionContractSnapshot, OptionSelection, OptionSelectionRequest


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

        filtered: list[OptionContractSnapshot] = []
        for contract in contracts:
            if contract.underlying_symbol != request.symbol:
                continue
            if contract.contract_type.upper() != allowed_type:
                continue
            if not (dte_min <= contract.dte <= dte_max):
                continue
            if (contract.open_interest or 0) < min_open_interest:
                continue
            if delta_min is not None and (contract.abs_delta is None or contract.abs_delta < float(delta_min)):
                continue
            if delta_max is not None and (contract.abs_delta is None or contract.abs_delta > float(delta_max)):
                continue
            if max_spread_pct is not None and (
                contract.spread_pct is None or contract.spread_pct > float(max_spread_pct)
            ):
                continue
            filtered.append(contract)

        if not filtered:
            raise ValueError(f"No contracts matched the execution profile for {request.deployment_id}")

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
