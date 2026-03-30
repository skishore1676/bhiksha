"""Position-tracking helpers for live and dry-run runtime checks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(slots=True)
class TrackedPosition:
    symbol: str
    deployment_id: str
    option_symbol: str | None = None
    quantity: int = 0
    source: str = "runtime"
    order_id: str | None = None
    stop_order_id: str | None = None


class PositionTracker:
    """Tracks active positions for risk checks and live-loop coordination."""

    def __init__(self) -> None:
        self._by_symbol: Counter[str] = Counter()
        self._by_deployment: Counter[str] = Counter()
        self._positions: list[TrackedPosition] = []

    @property
    def total_open_positions(self) -> int:
        return len(self._positions)

    def symbol_open_positions(self, symbol: str) -> int:
        return self._by_symbol[symbol]

    def deployment_open_positions(self, deployment_id: str) -> int:
        return self._by_deployment[deployment_id]

    def active_positions(self) -> list[TrackedPosition]:
        return list(self._positions)

    def replace_positions(self, positions: list[TrackedPosition]) -> None:
        self._positions = list(positions)
        self._rebuild_counters()

    def open_position(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        quantity: int = 0,
        source: str = "runtime",
        order_id: str | None = None,
        stop_order_id: str | None = None,
    ) -> None:
        for existing in self._positions:
            if (
                existing.symbol == symbol
                and existing.deployment_id == deployment_id
                and existing.option_symbol == option_symbol
            ):
                existing.quantity = quantity
                existing.source = source
                existing.order_id = order_id
                existing.stop_order_id = stop_order_id
                self._rebuild_counters()
                return

        self._positions.append(
            TrackedPosition(
                symbol=symbol,
                deployment_id=deployment_id,
                option_symbol=option_symbol,
                quantity=quantity,
                source=source,
                order_id=order_id,
                stop_order_id=stop_order_id,
            )
        )
        self._rebuild_counters()

    def close_position(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> None:
        remaining: list[TrackedPosition] = []
        removed = False
        for position in self._positions:
            same_record = position.symbol == symbol and position.deployment_id == deployment_id
            if same_record and option_symbol is not None:
                same_record = position.option_symbol == option_symbol
            if same_record and order_id is not None:
                same_record = position.order_id == order_id
            if same_record and not removed:
                removed = True
                continue
            remaining.append(position)
        self._positions = remaining
        self._rebuild_counters()

    def _rebuild_counters(self) -> None:
        self._by_symbol = Counter(position.symbol for position in self._positions)
        self._by_deployment = Counter(position.deployment_id for position in self._positions)
