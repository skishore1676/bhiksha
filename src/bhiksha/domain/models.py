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
    # Attribution-only label for a profile-dispatched exit (e.g. "no_progress",
    # "target_1_partial"). None for a native/legacy thesis exit. Never read by
    # order-management logic — daily_report is the sole consumer (workplan #10).
    exit_rule: str | None = None
    # ITEM D (2026-07-08 hygiene batch): whether the position, AT ENTRY, had
    # enough size to express the profile ladder (the T1 60/40 split needs
    # >= 2 contracts -- see _partial_quantity). Frozen once at entry time from
    # the ORIGINAL quantity and preserved thereafter (COALESCE, like
    # exit_rule) -- trade_sessions.quantity itself is overwritten to the
    # residual after a partial bank, so it cannot be used at report time to
    # recover this. None only for rows written before this migration.
    # Metadata/reporting-only; never read by order-management logic.
    can_ladder: bool | None = None


@dataclass(slots=True, frozen=True)
class PartialFillRecord:
    """One durable row per banked partial leg of a position (ITEM B, 2026-07-08
    hygiene batch, exit-accounting audit).

    A profile ladder can bank more than one partial over a position's life
    (today only ``target_1_partial`` does), but ``trade_sessions`` holds only
    the CURRENT residual ``quantity`` -- ``_handle_partial_scale_locked``
    overwrites it on every bank, and ``mark_closed`` later records only the
    RUNNER's own exit truth. Without a separate durable row, the banked leg's
    fill price/quantity/timestamp has no home: only its ``order_id`` survives,
    in the append-only ``partial_scale_submission`` event, with no fill
    confirmation ever read back.

    Per-leg P&L reconstruction with this table: original position size =
    ``trade_sessions.quantity`` (final residual) + ``sum(closed_quantity)``
    here; banked-leg P&L = ``(fill_price - entry_price) * closed_quantity``;
    runner P&L is unchanged, from ``trade_sessions.exit_price`` /
    ``trade_sessions.quantity`` (the residual) as before.
    """

    id: int | None
    trade_id: str
    deployment_id: str
    symbol: str
    option_symbol: str | None
    closed_quantity: int
    order_id: str | None
    exit_rule: str | None
    submitted_at: datetime | None
    fill_price: float | None = None
    fill_quantity: int | None = None
    filled_at: datetime | None = None
    order_status: str | None = None
    order_type: str | None = None
    broker_payload: dict[str, Any] | None = None
    # Audit fix A.2 (2026-07-08): where this leg came from. "partial_scale" is
    # a deliberate profile T1 bank (_handle_partial_scale_locked);
    # "exit_cancel_race" is an involuntary partial discovered when a reprice
    # cancel raced a working exit order (readback showed nonzero
    # filledQuantity short of the full position). "exit_dead_status" (item #21,
    # 2026-07-09) is the same involuntary partial discovered by a routine poll
    # that found the exit order terminally dead (REJECTED/CANCELED/EXPIRED)
    # after a partial fill -- recorded before resubmitting the residual so the
    # dead-status branch cannot oversell.
    origin: str = "partial_scale"
    # Audit fix 3: enrichment-sweep bookkeeping. ``enrich_attempts`` counts
    # unresolved polls; once it reaches the sweep's max (or the order reads
    # back terminally dead with no fill) the row is marked abandoned with a
    # reason and never re-polled -- the sweep must not retry forever against
    # a degraded broker.
    enrich_attempts: int = 0
    abandoned_reason: str | None = None


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
