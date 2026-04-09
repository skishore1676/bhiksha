"""Position-tracking helpers for live and dry-run runtime checks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from bhiksha.domain.enums import ExitMode


@dataclass(slots=True)
class TrackedPosition:
    symbol: str
    deployment_id: str
    trade_id: str | None = None
    option_symbol: str | None = None
    quantity: int = 0
    entry_price: float | None = None
    underlying_entry_price: float | None = None
    entry_timestamp: datetime | None = None
    source: str = "runtime"
    order_id: str | None = None
    stop_order_id: str | None = None
    stop_price: float | None = None
    target_order_id: str | None = None
    target_price: float | None = None
    exit_order_id: str | None = None
    exit_limit_price: float | None = None
    exit_submitted_at: datetime | None = None
    exit_mode: ExitMode | None = None
    exit_reprice_count: int = 0


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

    def find_by_option_symbol(self, option_symbol: str) -> list[TrackedPosition]:
        return [position for position in self._positions if position.option_symbol == option_symbol]

    def replace_positions(self, positions: list[TrackedPosition]) -> None:
        self._positions = list(positions)
        self._rebuild_counters()

    def open_position(
        self,
        symbol: str,
        deployment_id: str,
        *,
        trade_id: str | None = None,
        option_symbol: str | None = None,
        quantity: int = 0,
        entry_price: float | None = None,
        underlying_entry_price: float | None = None,
        entry_timestamp: datetime | None = None,
        source: str = "runtime",
        order_id: str | None = None,
        stop_order_id: str | None = None,
        stop_price: float | None = None,
        target_order_id: str | None = None,
        target_price: float | None = None,
        exit_order_id: str | None = None,
        exit_limit_price: float | None = None,
        exit_submitted_at: datetime | None = None,
        exit_mode: ExitMode | None = None,
        exit_reprice_count: int = 0,
    ) -> None:
        for existing in self._positions:
            if (
                existing.symbol == symbol
                and existing.deployment_id == deployment_id
                and existing.trade_id == trade_id
                and existing.option_symbol == option_symbol
            ):
                existing.trade_id = trade_id
                existing.quantity = quantity
                existing.entry_price = entry_price
                existing.underlying_entry_price = underlying_entry_price
                existing.entry_timestamp = entry_timestamp
                existing.source = source
                existing.order_id = order_id
                existing.stop_order_id = stop_order_id
                existing.stop_price = stop_price
                existing.target_order_id = target_order_id
                existing.target_price = target_price
                existing.exit_order_id = exit_order_id
                existing.exit_limit_price = exit_limit_price
                existing.exit_submitted_at = exit_submitted_at
                existing.exit_mode = exit_mode
                existing.exit_reprice_count = exit_reprice_count
                self._rebuild_counters()
                return

        self._positions.append(
            TrackedPosition(
                symbol=symbol,
                deployment_id=deployment_id,
                trade_id=trade_id,
                option_symbol=option_symbol,
                quantity=quantity,
                entry_price=entry_price,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=entry_timestamp,
                source=source,
                order_id=order_id,
                stop_order_id=stop_order_id,
                stop_price=stop_price,
                target_order_id=target_order_id,
                target_price=target_price,
                exit_order_id=exit_order_id,
                exit_limit_price=exit_limit_price,
                exit_submitted_at=exit_submitted_at,
                exit_mode=exit_mode,
                exit_reprice_count=exit_reprice_count,
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
