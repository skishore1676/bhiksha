"""Contract selection logic for single-leg option trades."""

from __future__ import annotations

from bhiksha.domain.models import OptionContractSnapshot, OptionSelection, OptionSelectionRequest


class SelectorEmptyError(ValueError):
    """No contract survived the execution-profile filters.

    Carries a per-filter elimination breakdown so the operator can see which
    constraint emptied the candidate set instead of just "no contracts".
    """

    def __init__(self, deployment_id: str, breakdown: dict[str, int], diagnostics: dict | None = None) -> None:
        self.deployment_id = deployment_id
        self.breakdown = breakdown
        self.diagnostics = diagnostics or {}
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
        dte_fallback_policy = str(request.execution_params.get("dte_fallback_policy") or "strict").strip().lower()

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
        desired_contracts: list[OptionContractSnapshot] = []
        dte_window_candidates = 0
        for contract in contracts:
            if contract.underlying_symbol != request.symbol:
                eliminated["wrong_underlying"] += 1
                continue
            if contract.contract_type.upper() != allowed_type:
                eliminated["wrong_contract_type"] += 1
                continue
            desired_contracts.append(contract)
            if not (dte_min <= contract.dte <= dte_max):
                eliminated["dte_out_of_range"] += 1
                continue
            dte_window_candidates += 1
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

        fallback_policy_applied: str | None = None
        if not filtered and dte_window_candidates == 0 and dte_fallback_policy == "allow_nearest_after":
            filtered = self._nearest_after_candidates(
                desired_contracts,
                dte_max=dte_max,
                delta_min=delta_min,
                delta_max=delta_max,
                min_open_interest=min_open_interest,
                max_spread_pct=max_spread_pct,
            )
            if filtered:
                fallback_policy_applied = dte_fallback_policy

        if not filtered:
            raise SelectorEmptyError(
                request.deployment_id,
                eliminated,
                diagnostics={
                    "requested_dte_min": dte_min,
                    "requested_dte_max": dte_max,
                    "available_dtes": sorted({contract.dte for contract in desired_contracts}),
                    "dte_window_candidates": dte_window_candidates,
                    "dte_fallback_policy": dte_fallback_policy,
                    "nearest_after_dte": self._nearest_after_dte(desired_contracts, dte_max=dte_max),
                },
            )

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
        percentile_cohort = [
            contract
            for contract in desired_contracts
            if contract.open_interest is not None
            and (
                contract.dte == chosen.dte
                if fallback_policy_applied
                else dte_min <= contract.dte <= dte_max
            )
        ]
        return OptionSelection(
            option_symbol=chosen.option_symbol,
            contract_type=chosen.contract_type,
            dte=chosen.dte,
            abs_delta=chosen.abs_delta,
            bid=chosen.bid,
            ask=chosen.ask,
            strike=chosen.strike,
            open_interest=chosen.open_interest,
            open_interest_percentile=self._open_interest_percentile(chosen, percentile_cohort),
            dte_fallback_policy=fallback_policy_applied,
            requested_dte_min=dte_min if fallback_policy_applied else None,
            requested_dte_max=dte_max if fallback_policy_applied else None,
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

    def _nearest_after_candidates(
        self,
        contracts: list[OptionContractSnapshot],
        *,
        dte_max: int,
        delta_min: float | None,
        delta_max: float | None,
        min_open_interest: int,
        max_spread_pct: float | None,
    ) -> list[OptionContractSnapshot]:
        for dte in sorted({contract.dte for contract in contracts if contract.dte > dte_max}):
            candidates = [
                contract
                for contract in contracts
                if contract.dte == dte
                and self._passes_non_dte_filters(
                    contract,
                    delta_min=delta_min,
                    delta_max=delta_max,
                    min_open_interest=min_open_interest,
                    max_spread_pct=max_spread_pct,
                )
            ]
            if candidates:
                return candidates
        return []

    @staticmethod
    def _passes_non_dte_filters(
        contract: OptionContractSnapshot,
        *,
        delta_min: float | None,
        delta_max: float | None,
        min_open_interest: int,
        max_spread_pct: float | None,
    ) -> bool:
        if (contract.open_interest or 0) < min_open_interest:
            return False
        if delta_min is not None and (contract.abs_delta is None or contract.abs_delta < float(delta_min)):
            return False
        if delta_max is not None and (contract.abs_delta is None or contract.abs_delta > float(delta_max)):
            return False
        if max_spread_pct is not None and (
            contract.spread_pct is None or contract.spread_pct > float(max_spread_pct)
        ):
            return False
        return True

    @staticmethod
    def _nearest_after_dte(contracts: list[OptionContractSnapshot], *, dte_max: int) -> int | None:
        later_dtes = sorted({contract.dte for contract in contracts if contract.dte > dte_max})
        return later_dtes[0] if later_dtes else None

    @staticmethod
    def _open_interest_percentile(
        chosen: OptionContractSnapshot,
        cohort: list[OptionContractSnapshot],
    ) -> float | None:
        if chosen.open_interest is None or not cohort:
            return None
        at_or_below = sum(
            1
            for contract in cohort
            if contract.open_interest is not None and contract.open_interest <= chosen.open_interest
        )
        return at_or_below / len(cohort)
