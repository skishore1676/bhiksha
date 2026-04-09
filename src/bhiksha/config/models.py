"""Typed configuration models for Bhiksha."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

RangeValueT = TypeVar("RangeValueT", int, float)


def _parse_compact_range(
    data: dict[str, Any],
    *,
    field_name: str,
    minimum_name: str,
    maximum_name: str,
    caster: Callable[[str], RangeValueT],
) -> None:
    if field_name not in data or minimum_name in data or maximum_name in data:
        return
    raw_value = data.pop(field_name)
    if raw_value is None:
        return
    if isinstance(raw_value, (int, float)):
        value = caster(str(raw_value))
        data[minimum_name] = value
        data[maximum_name] = value
        return
    if not isinstance(raw_value, str):
        raise TypeError(f"{field_name} must be a string or number")
    value = raw_value.strip()
    if not value:
        return
    if "-" in value:
        start_raw, end_raw = (part.strip() for part in value.split("-", 1))
        data[minimum_name] = caster(start_raw)
        data[maximum_name] = caster(end_raw)
        return
    parsed = caster(value)
    data[minimum_name] = parsed
    data[maximum_name] = parsed


def _parse_time_window(data: dict[str, Any], *, field_name: str, start_name: str, end_name: str) -> None:
    if field_name not in data or start_name in data or end_name in data:
        return
    raw_value = data.pop(field_name)
    if raw_value is None:
        return
    if not isinstance(raw_value, str):
        raise TypeError(f"{field_name} must be a HH:MM-HH:MM string")
    value = raw_value.strip()
    if not value:
        return
    if "-" not in value:
        raise ValueError(f"{field_name} must be formatted as HH:MM-HH:MM")
    start_raw, end_raw = (part.strip() for part in value.split("-", 1))
    data[start_name] = start_raw
    data[end_name] = end_raw


class AppConfig(BaseModel):
    app_name: str = "bhiksha"
    timezone: str = "America/New_York"
    warmup_trading_days: int = 2
    dry_run: bool = True
    event_bus: str = "in_memory"
    sqlite_path: str = "bhiksha.db"
    rolling_bar_capacity: int = 20000
    bar_poll_interval_seconds: int = 15
    reconciliation_interval_seconds: int = 15
    order_fill_poll_seconds: int = 2
    order_fill_timeout_seconds: int = 20
    generated_deployments_dir: str = "config/deployments/generated"
    strategy_catalog_dir: str = "config/strategy_catalog"
    deployment_selection_mode: Literal["all", "manual_only", "generated_only", "prefer_generated"] = "all"
    bias_inputs_path: str = "config/bias_inputs.yaml"
    playbook_artifacts_dir: str = "artifacts/playbook"
    observation_reports_dir: str = "artifacts/observations"


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
    entry_window_start_et: str | None = None
    entry_window_end_et: str | None = None
    shadow_only: bool = False

    @model_validator(mode="before")
    @classmethod
    def expand_compact_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        _parse_compact_range(
            normalized,
            field_name="dte",
            minimum_name="dte_min",
            maximum_name="dte_max",
            caster=int,
        )
        _parse_compact_range(
            normalized,
            field_name="delta_target",
            minimum_name="target_abs_delta_min",
            maximum_name="target_abs_delta_max",
            caster=float,
        )
        _parse_time_window(
            normalized,
            field_name="entry_window_et",
            start_name="entry_window_start_et",
            end_name="entry_window_end_et",
        )
        return normalized


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
    thesis_exit_anchor: str | None = None
    thesis_exit_policy: str | None = None
    thesis_exit_params: dict[str, Any] = Field(default_factory=dict)
    catastrophe_exit_anchor: str | None = "option_premium"
    catastrophe_exit_params: dict[str, Any] = Field(default_factory=dict)


class SourceSpec(BaseModel):
    origin: str | None = None
    run_date: str | None = None
    artifact: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeploymentManifest(BaseModel):
    deployment_id: str
    enabled: bool = True
    symbol: str
    strategy: StrategySpec
    execution: ExecutionSpec
    risk: RiskSpec
    exit: ExitSpec = Field(default_factory=ExitSpec)
    source: SourceSpec = Field(default_factory=SourceSpec)
    config_path: str | None = Field(default=None, exclude=True)
    source_kind: Literal["manual", "generated", "active_plan"] | None = Field(default=None, exclude=True)


class ActivePlan(BaseModel):
    contract_name: str = "active_plan"
    schema_version: int = 1
    active_plan_id: str
    trading_date: str | None = None
    generated_at: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    suppressed: list[dict[str, Any]] = Field(default_factory=list)
    deployments: list[DeploymentManifest] = Field(default_factory=list)


class StrategyCatalogEntry(BaseModel):
    strategy_id: str
    enabled: bool = True
    symbol: str
    strategy: StrategySpec
    execution: ExecutionSpec
    risk: RiskSpec
    exit: ExitSpec = Field(default_factory=ExitSpec)
    source: SourceSpec = Field(default_factory=SourceSpec)
    approval_status: Literal["draft", "approved", "retired"] = "approved"
    tags: list[str] = Field(default_factory=list)
    config_path: str | None = Field(default=None, exclude=True)


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


class BiasSelection(BaseModel):
    allowed_bias_templates: ClassVar[set[str]] = {
        "bullish_trend_intraday",
        "bullish_mean_reversion_intraday",
        "bearish_trend_intraday",
        "bearish_mean_reversion_intraday",
    }

    symbol: str
    bias_template: str
    horizon: str = "intraday"
    enabled: bool = True
    max_active_candidates: int = 1

    @model_validator(mode="after")
    def validate_selection(self) -> "BiasSelection":
        self.symbol = self.symbol.upper()
        if self.bias_template not in self.allowed_bias_templates:
            raise ValueError(f"Unsupported bias_template: {self.bias_template}")
        if self.horizon != "intraday":
            raise ValueError(f"Unsupported horizon: {self.horizon}")
        if self.max_active_candidates < 1:
            raise ValueError("max_active_candidates must be >= 1")
        return self


class EmergencyBiasControl(BaseModel):
    halt_and_flatten: bool = False


class BiasConfig(BaseModel):
    emergency: EmergencyBiasControl = Field(default_factory=EmergencyBiasControl)
    selections: list[BiasSelection] = Field(default_factory=list)
