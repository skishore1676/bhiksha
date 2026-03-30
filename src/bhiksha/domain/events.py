"""Event contracts for the internal runtime bus."""

from __future__ import annotations

from dataclasses import dataclass

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
