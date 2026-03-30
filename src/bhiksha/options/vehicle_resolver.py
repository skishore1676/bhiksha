"""Single-leg option vehicle resolution."""

from __future__ import annotations

from bhiksha.domain.models import OptionContractSnapshot, OptionSelection, OptionSelectionRequest
from bhiksha.options.selectors import SingleLegOptionSelector


class VehicleResolver:
    """Translate an underlying signal into a specific option contract.

    Contract discovery will be wired once option-chain providers are in place.
    """

    def __init__(self, selector: SingleLegOptionSelector | None = None) -> None:
        self.selector = selector or SingleLegOptionSelector()

    def resolve(
        self,
        request: OptionSelectionRequest,
        contracts: list[OptionContractSnapshot],
    ) -> OptionSelection:
        return self.selector.select(request, contracts)

