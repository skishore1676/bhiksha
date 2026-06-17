"""Core domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bhiksha.domain.enums import ExitMode, SignalDirection


@dataclass(slots=True, frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True, frozen=True)
class SignalDecision:
    deployment_id: str
    symbol: str
    timestamp: datetime
    signal: bool
    direction: SignalDirection | None = None
    reason: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ExitDecision:
    deployment_id: str
    symbol: str
    timestamp: datetime
    exit: bool
    action: str = "hold"
    reason: list[str] = field(default_factory=list)
    cancel_protection_orders: bool = False
    replacement_stop_price: float | None = None
    target_price: float | None = None
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ExitPlan:
    trade_id: str
    deployment_id: str
    symbol: str
    option_symbol: str
    quantity: int
    action: str
    reasons: list[str]
    dry_run: bool = True
    order_id: str | None = None
    canceled_stop_order_id: str | None = None
    canceled_target_order_id: str | None = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class OptionSelectionRequest:
    deployment_id: str
    symbol: str
    direction: SignalDirection
    signal_timestamp: datetime
    execution_profile: str
    execution_params: dict[str, Any]


@dataclass(slots=True, frozen=True)
class OptionContractSnapshot:
    option_symbol: str
    underlying_symbol: str
    contract_type: str
    expiration_date: str
    dte: int
    strike: float
    delta: float | None
    bid: float | None
    ask: float | None
    open_interest: int | None = None

    @property
    def abs_delta(self) -> float | None:
        if self.delta is None:
            return None
        return abs(self.delta)

    @property
    def spread_pct(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return (self.ask - self.bid) / mid


@dataclass(slots=True, frozen=True)
class OptionSelection:
    option_symbol: str
    contract_type: str
    dte: int
    abs_delta: float | None
    bid: float | None = None
    ask: float | None = None
    strike: float | None = None
    dte_fallback_policy: str | None = None
    requested_dte_min: int | None = None
    requested_dte_max: int | None = None

    @property
    def estimated_entry_price(self) -> float | None:
        if self.ask is not None:
            return self.ask
        return self.bid


@dataclass(slots=True, frozen=True)
class TradePlan:
    trade_id: str
    deployment_id: str
    symbol: str
    direction: SignalDirection
    option_symbol: str
    quantity: int
    estimated_entry_price: float
    risk_reasons: list[str]
    dry_run: bool = True
    order_id: str | None = None
    stop_order_id: str | None = None
    target_order_id: str | None = None
    underlying_entry_price: float | None = None
    entry_timestamp: datetime | None = None
    risk_details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class TradeRecord:
    trade_id: str
    deployment_id: str
    symbol: str
    option_symbol: str | None = None
    quantity: int = 0
    entry_price: float | None = None
    underlying_entry_price: float | None = None
    entry_timestamp: datetime | None = None
    status: str = "pending_entry"
    entry_order_id: str | None = None
    stop_order_id: str | None = None
    stop_price: float | None = None
    target_order_id: str | None = None
    target_price: float | None = None
    exit_order_id: str | None = None
    exit_limit_price: float | None = None
    exit_submitted_at: datetime | None = None
    exit_mode: ExitMode | None = None
    exit_price: float | None = None
    exit_filled_quantity: int | None = None
    exit_filled_at: datetime | None = None
    exit_order_status: str | None = None
    exit_order_type: str | None = None
    exit_broker_payload: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class CashBudgetDay:
    trade_date: str
    account_type: str | None
    broker_cash_only_buying_power: float
    usable_budget: float
    buffer_pct: float


@dataclass(slots=True, frozen=True)
class CashBudgetReservation:
    trade_id: str
    trade_date: str
    amount: float
    status: str
