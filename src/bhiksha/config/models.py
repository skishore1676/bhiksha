"""Typed configuration models for Bhiksha."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    app_name: str = "bhiksha"
    timezone: str = "America/New_York"
    warmup_trading_days: int = 2
    dry_run: bool = True
    event_bus: str = "in_memory"
    sqlite_path: str = "bhiksha.db"
    rolling_bar_capacity: int = 20000
    bar_poll_interval_seconds: int = 15
    order_fill_poll_seconds: int = 2
    order_fill_timeout_seconds: int = 20


class ProviderConfig(BaseModel):
    underlying_live_primary: str = "schwab"
    underlying_backfill_primary: str = "polygon"
    execution_broker_primary: str = "public"


class StrategySpec(BaseModel):
    key: str
    version: int = 1
    params: dict[str, Any] = Field(default_factory=dict)


class ExecutionSpec(BaseModel):
    profile: str
    option_mapping: dict[str, str] = Field(default_factory=dict)
    dte_min: int = 0
    dte_max: int = 7
    target_abs_delta_min: float | None = None
    target_abs_delta_max: float | None = None
    min_open_interest: int = 0
    max_bid_ask_spread_pct: float | None = None


class RiskSpec(BaseModel):
    profile: str
    max_trade_premium_usd: float | None = None
    hard_flat_time_et: str | None = None
    stop_loss_pct: float = 0.45


class ExitSpec(BaseModel):
    profile: str = "strategy_managed_v1"
    use_algorithmic_exit: bool = True
    use_profit_target: bool = False
    profit_target_multiple: float | None = None
    target_approach_offset_pct: float | None = None
    target_pullback_restore_progress_pct: float | None = None
    stop_loss_pct: float = 0.45
    stop_to_breakeven_after_r_multiple: float | None = None
    hard_flat_time_et: str = "15:55"


class SourceSpec(BaseModel):
    origin: str | None = None
    run_date: str | None = None
    artifact: str | None = None


class DeploymentManifest(BaseModel):
    deployment_id: str
    enabled: bool = True
    symbol: str
    strategy: StrategySpec
    execution: ExecutionSpec
    risk: RiskSpec
    exit: ExitSpec = Field(default_factory=ExitSpec)
    source: SourceSpec = Field(default_factory=SourceSpec)


class VehicleProfile(BaseModel):
    profile: str
    long_signal_contract_type: str = "CALL"
    short_signal_contract_type: str = "PUT"
    dte_min: int = 0
    dte_max: int = 7
    target_abs_delta_min: float | None = None
    target_abs_delta_max: float | None = None
    min_open_interest: int = 0
    max_bid_ask_spread_pct: float | None = None


class ConservativeRiskProfile(BaseModel):
    profile: str = "conservative_day1"
    max_open_positions_total: int = 2
    max_open_positions_per_symbol: int = 1
    max_open_positions_per_deployment: int = 1
    max_trade_premium_usd: float = 300.0
    max_daily_drawdown_pct: float = 2.0
    hard_flat_time_et: str = "15:55"
