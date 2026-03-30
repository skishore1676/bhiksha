"""Minimal trade lifecycle state store."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bhiksha.state.position_tracker import TrackedPosition


class LifecycleState(StrEnum):
    PENDING_ENTRY = "pending_entry"
    OPEN_UNPROTECTED = "open_unprotected"
    OPEN_PROTECTED = "open_protected"
    TARGET_ACTIVE = "target_active"
    EXIT_PENDING = "exit_pending"
    CLOSED = "closed"
    RECONCILIATION_HOLD = "reconciliation_hold"


@dataclass(slots=True)
class TradeLifecycle:
    symbol: str
    deployment_id: str
    state: LifecycleState
    option_symbol: str | None = None
    order_id: str | None = None


class TradeLifecycleStore:
    """Tracks allowed lifecycle transitions for one deployment/symbol slot."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], TradeLifecycle] = {}

    def get(self, symbol: str, deployment_id: str) -> TradeLifecycle | None:
        return self._records.get((symbol, deployment_id))

    def can_submit_entry(self, symbol: str, deployment_id: str) -> bool:
        record = self.get(symbol, deployment_id)
        return record is None or record.state == LifecycleState.CLOSED

    def begin_entry(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> None:
        self._records[(symbol, deployment_id)] = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.PENDING_ENTRY,
            option_symbol=option_symbol,
            order_id=order_id,
        )

    def mark_open(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
        protected: bool,
    ) -> None:
        self._records[(symbol, deployment_id)] = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.OPEN_PROTECTED if protected else LifecycleState.OPEN_UNPROTECTED,
            option_symbol=option_symbol,
            order_id=order_id,
        )

    def mark_target_active(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> None:
        self._records[(symbol, deployment_id)] = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.TARGET_ACTIVE,
            option_symbol=option_symbol,
            order_id=order_id,
        )

    def mark_exit_pending(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> None:
        self._records[(symbol, deployment_id)] = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.EXIT_PENDING,
            option_symbol=option_symbol,
            order_id=order_id,
        )

    def mark_closed(self, symbol: str, deployment_id: str) -> None:
        self._records[(symbol, deployment_id)] = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.CLOSED,
        )

    def sync_from_positions(self, positions: list[TrackedPosition]) -> None:
        active_keys: set[tuple[str, str]] = set()
        for position in positions:
            key = (position.symbol, position.deployment_id)
            active_keys.add(key)
            if position.target_order_id:
                self.mark_target_active(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                    order_id=position.target_order_id or position.order_id,
                )
            else:
                self.mark_open(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                    order_id=position.order_id,
                    protected=bool(position.stop_order_id),
                )
        for key, record in list(self._records.items()):
            if key not in active_keys and record.state in {
                LifecycleState.OPEN_UNPROTECTED,
                LifecycleState.OPEN_PROTECTED,
                LifecycleState.TARGET_ACTIVE,
                LifecycleState.EXIT_PENDING,
            }:
                self._records[key] = TradeLifecycle(
                    symbol=record.symbol,
                    deployment_id=record.deployment_id,
                    state=LifecycleState.CLOSED,
                )
