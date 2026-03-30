from __future__ import annotations
from typing import Protocol, Optional, Dict, List, TypedDict


class Quote(TypedDict, total=False):
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]


class BrokerClient(Protocol):
    async def get_portfolio(self) -> Dict:
        ...

    async def get_positions(self) -> List[Dict]:
        ...

    async def get_orders(self) -> List[Dict]:
        ...

    async def get_order(self, order_id: str) -> Dict:
        ...

    async def cancel_order(self, order_id: str) -> None:
        ...

    async def place_order(self, order: Dict) -> Dict:
        """
        Temporarily accept a generic dict for order while we introduce DomainOrder later.
        """
        ...

    async def get_stock_quote(self, symbol: str) -> Optional[float]:
        ...

    async def get_option_quote(self, symbol: str) -> Optional[Quote]:
        ...

    async def get_option_chain(self, symbol: str, expiration: str) -> Dict:
        ...
