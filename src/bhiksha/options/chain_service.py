"""Option-chain provider interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from bhiksha.domain.models import OptionContractSnapshot


class OptionChainService(ABC):
    @abstractmethod
    async def get_chain(
        self,
        symbol: str,
        *,
        contract_type: str = "ALL",
        from_date: date | None = None,
        to_date: date | None = None,
        strike_count: int = 20,
    ) -> list[OptionContractSnapshot]:
        """Return normalized contract snapshots for the given underlying."""
