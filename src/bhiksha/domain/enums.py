"""Domain enums used across the runtime."""

from __future__ import annotations

from enum import Enum


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class PositionState(str, Enum):
    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    CLOSED = "closed"
    HALTED = "halted"
    ERROR = "error"


class OrderState(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

