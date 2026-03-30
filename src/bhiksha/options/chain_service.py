"""Option-chain provider interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod

from bhiksha.domain.models import OptionContractSnapshot


class OptionChainService(ABC):
    @abstractmethod
    async def get_chain(self, symbol: str) -> list[OptionContractSnapshot]:
        """Return normalized contract snapshots for the given underlying."""

