"""Exit Engine V2 durable policy, state, and action-intent records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TradeExitPolicySnapshot:
    trade_id: str
    deployment_id: str
    option_symbol: str | None
    active_plan_id: str | None
    startup_config_id: str | None
    policy_schema_version: str
    policy_id: str
    policy_hash: str
    canonical_policy: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    frozen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExitRuntimeState:
    trade_id: str
    deployment_id: str
    option_symbol: str | None
    policy_hash: str
    seed_entry_premium: float
    seed_quantity: int
    initial_risk_per_contract: float
    raw_peak_premium: float
    confirmed_peak_r: float
    peak_timestamp: datetime | None = None
    locked_floor_r: float | None = None
    committed_stop_price: float | None = None
    target_1_banked: bool = False
    banked_quantity: int = 0
    breakeven_emitted: bool = False
    runner_state: str = "pre_t1"
    recovery_status: str = "active"
    degraded_reason: str | None = None
    last_evaluated_at: datetime | None = None
    state_version: int = 1


@dataclass(frozen=True, slots=True)
class ExitActionIntent:
    idempotency_key: str
    trade_id: str
    policy_hash: str
    action_kind: str
    action_slot: str
    expected_state_version: int
    requested_quantity: int | None = None
    requested_stop_price: float | None = None
    status: str = "prepared"
    broker_order_id: str | None = None
    broker_payload: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
