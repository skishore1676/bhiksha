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


@dataclass(slots=True)
class LifecycleTransition:
    symbol: str
    deployment_id: str
    previous_state: LifecycleState | None
    new_state: LifecycleState
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
    ) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.PENDING_ENTRY,
            option_symbol=option_symbol,
            order_id=order_id,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def mark_open(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
        protected: bool,
    ) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.OPEN_PROTECTED if protected else LifecycleState.OPEN_UNPROTECTED,
            option_symbol=option_symbol,
            order_id=order_id,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def mark_target_active(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.TARGET_ACTIVE,
            option_symbol=option_symbol,
            order_id=order_id,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def mark_exit_pending(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.EXIT_PENDING,
            option_symbol=option_symbol,
            order_id=order_id,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def mark_closed(self, symbol: str, deployment_id: str) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.CLOSED,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def mark_reconciliation_hold(
        self,
        symbol: str,
        deployment_id: str,
        *,
        option_symbol: str | None = None,
        order_id: str | None = None,
    ) -> LifecycleTransition | None:
        previous = self.get(symbol, deployment_id)
        next_record = TradeLifecycle(
            symbol=symbol,
            deployment_id=deployment_id,
            state=LifecycleState.RECONCILIATION_HOLD,
            option_symbol=option_symbol,
            order_id=order_id,
        )
        self._records[(symbol, deployment_id)] = next_record
        return _transition(previous, next_record)

    def sync_from_positions(self, positions: list[TrackedPosition]) -> list[LifecycleTransition]:
        transitions: list[LifecycleTransition] = []
        active_keys: set[tuple[str, str]] = set()
        for position in positions:
            key = (position.symbol, position.deployment_id)
            active_keys.add(key)
            if position.exit_mode is not None or position.exit_order_id is not None:
                transition = self.mark_exit_pending(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                    order_id=position.exit_order_id or position.order_id,
                )
            elif position.target_order_id:
                transition = self.mark_target_active(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                    order_id=position.target_order_id or position.order_id,
                )
            else:
                transition = self.mark_open(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                    order_id=position.order_id,
                    protected=bool(position.stop_order_id),
                )
            if transition is not None:
                transitions.append(transition)
        for key, record in list(self._records.items()):
            if key not in active_keys and record.state in {
                LifecycleState.OPEN_UNPROTECTED,
                LifecycleState.OPEN_PROTECTED,
                LifecycleState.TARGET_ACTIVE,
                LifecycleState.EXIT_PENDING,
            }:
                next_record = TradeLifecycle(
                    symbol=record.symbol,
                    deployment_id=record.deployment_id,
                    state=LifecycleState.CLOSED,
                )
                self._records[key] = next_record
                transition = _transition(record, next_record)
                if transition is not None:
                    transitions.append(transition)
        return transitions


def _transition(previous: TradeLifecycle | None, current: TradeLifecycle) -> LifecycleTransition | None:
    previous_state = previous.state if previous is not None else None
    if (
        previous is not None
        and previous.state == current.state
        and previous.option_symbol == current.option_symbol
        and previous.order_id == current.order_id
    ):
        return None
    return LifecycleTransition(
        symbol=current.symbol,
        deployment_id=current.deployment_id,
        previous_state=previous_state,
        new_state=current.state,
        option_symbol=current.option_symbol,
        order_id=current.order_id,
    )
