"""Event contracts for the internal runtime bus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bhiksha.domain.models import Bar, ExitDecision, SignalDecision


@dataclass(slots=True, frozen=True)
class BarClosedEvent:
    symbol: str
    timeframe: str
    provider: str
    bar: Bar


@dataclass(slots=True, frozen=True)
class SignalEvaluatedEvent:
    decision: SignalDecision


@dataclass(slots=True, frozen=True)
class ExitEvaluatedEvent:
    decision: ExitDecision


@dataclass(slots=True, frozen=True)
class TradeLifecycleTransitionEvent:
    symbol: str
    deployment_id: str
    timestamp: datetime
    previous_state: str | None
    new_state: str
    option_symbol: str | None = None
    order_id: str | None = None
    reason: str | None = None
