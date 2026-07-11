"""Typed configuration models for Bhiksha."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

from bhiksha.strategy.capabilities import supports_native_algorithmic_exit
from bhiksha.time_utils import normalize_time_text, parse_time_text

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


def _normalize_time_field(data: dict[str, Any], field_name: str) -> None:
    if field_name not in data:
        return
    data[field_name] = normalize_time_text(data.get(field_name))


def _normalize_nested_time_field(data: dict[str, Any], field_name: str, nested_key: str) -> None:
    raw_value = data.get(field_name)
    if not isinstance(raw_value, dict):
        return
    normalized = dict(raw_value)
    normalized[nested_key] = normalize_time_text(normalized.get(nested_key))
    data[field_name] = normalized


def _has_exit_fallback_protection(exit_spec: "ExitSpec") -> bool:
    return (exit_spec.stop_loss_pct is not None and exit_spec.stop_loss_pct > 0) or bool(exit_spec.thesis_exit_policy) or (
        exit_spec.use_profit_target
        and (exit_spec.profit_target_multiple is not None or exit_spec.option_profit_target_pct is not None)
    )


def _validate_exit_safety(
    *,
    identifier: str,
    strategy_key: str,
    exit_spec: "ExitSpec",
) -> None:
    if not exit_spec.use_algorithmic_exit:
        return
    if supports_native_algorithmic_exit(strategy_key):
        return
    if _has_exit_fallback_protection(exit_spec):
        return
    raise ValueError(
        f"{identifier} enables use_algorithmic_exit for strategy {strategy_key!r}, "
        "but that strategy has no native exit implementation and no stop/thesis/profit-target fallback"
    )


def _validate_profile_recovery_stop(
    *,
    identifier: str,
    exit_spec: "ExitSpec",
    risk_spec: "RiskSpec",
) -> None:
    """HIGH-2: a profile-exit deployment must have a resolvable recovery stop pct.

    When a deployment pins an exit profile (``profile_exit_id`` set), the live
    runtime's "re-arm protection next tick" path (``_restore_missing_protection``
    -> ``_resolved_recovery_stop_loss_pct``) must be able to derive a positive
    stop %; otherwise it silently NO-OPs and the position rides NAKED. A resolvable
    stop comes from any of (matching the runtime resolver order): the deployment
    ``exit.stop_loss_pct``, the global ``risk.stop_loss_pct``, or — since the
    profile supplies its own floor — the profile ``initial_stop_pct`` / wider
    ``premium_disaster_stop_pct``. Reject the config when none is positive.
    """
    if not getattr(exit_spec, "profile_exit_id", None):
        return

    def _positive(value: float | None) -> bool:
        return value is not None and value > 0

    if (
        _positive(exit_spec.stop_loss_pct)
        or _positive(risk_spec.stop_loss_pct)
        or _positive(exit_spec.initial_stop_pct)
        or _positive(exit_spec.premium_disaster_stop_pct)
    ):
        return
    raise ValueError(
        f"{identifier} pins exit profile {exit_spec.profile_exit_id!r} but has no "
        "resolvable recovery stop pct: set a positive exit.stop_loss_pct or "
        "risk.stop_loss_pct (or a profile initial_stop_pct/premium_disaster_stop_pct). "
        "Without one the live re-arm path would no-op into a naked ride."
    )


def _validate_optional_time_field(data: Any, field_name: str) -> None:
    if not isinstance(data, dict):
        return
    raw_value = data.get(field_name)
    if raw_value is None:
        return
    normalized = normalize_time_text(raw_value)
    if normalized is None:
        data[field_name] = None
        return
    parse_time_text(normalized)
    data[field_name] = normalized


def _validate_nested_optional_time_field(data: Any, field_name: str, nested_key: str) -> None:
    if not isinstance(data, dict):
        return
    nested = data.get(field_name)
    if not isinstance(nested, dict):
        return
    normalized_nested = dict(nested)
    _validate_optional_time_field(normalized_nested, nested_key)
    data[field_name] = normalized_nested


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
    reconciliation_max_staleness_seconds: int = 60
    order_fill_poll_seconds: int = 2
    order_fill_timeout_seconds: int = 20
    entry_reprice_enabled: bool = False
    entry_reprice_checkpoints_seconds: list[int] = Field(default_factory=lambda: [30, 90])
    entry_reprice_cancel_after_seconds: int = 180
    entry_reprice_spread_pcts: list[float] = Field(default_factory=lambda: [0.50, 1.00])
    generated_deployments_dir: str = "config/deployments/generated"
    strategy_catalog_dir: str = "config/strategy_catalog"
    deployment_selection_mode: Literal["all", "manual_only", "generated_only", "prefer_generated"] = "all"
    bias_inputs_path: str = "config/bias_inputs.yaml"
    playbook_artifacts_dir: str = "artifacts/playbook"
    observation_reports_dir: str = "artifacts/observations"
    # Prospective paired-exit collection is observational and OFF by default.
    # It consumes only quotes already requested by the runtime and has no
    # execution/risk authority.
    exit_edge_live_shadow_enabled: bool = False
    exit_edge_live_shadow_db_path: str = "artifacts/observations/exit_edge_live.sqlite3"
    exit_edge_live_shadow_status_path: str = "artifacts/observations/exit_edge_live_status.json"
    exit_edge_live_shadow_queue_capacity: int = Field(default=512, ge=1, le=100_000)
    exit_edge_live_shadow_fill_latency_ms: int = Field(default=0, ge=0)
    exit_edge_live_shadow_max_freshness_ms: int = Field(default=2_000, ge=0)
    exit_edge_live_shadow_max_sequence_gap: int = Field(default=1, ge=1)


class ProviderConfig(BaseModel):
    underlying_live_primary: str = "schwab"
    underlying_backfill_primary: str = "schwab"
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
    dte_fallback_policy: Literal["strict", "allow_nearest_after"] = "strict"
    target_abs_delta_min: float | None = None
    target_abs_delta_max: float | None = None
    min_open_interest: int = 0
    max_bid_ask_spread_pct: float | None = None
    entry_pricing_mode: Literal["passive", "balanced", "urgent", "cross"] = "urgent"
    entry_pricing_urgent_spread_pct: float = 0.25
    entry_pricing_passive_spread_pct: float = 0.25
    entry_pricing_cross_tight_spread_pct: float = 0.03
    entry_pricing_require_two_sided_quote: bool = True
    entry_pricing_require_open_interest: bool = True
    entry_window_start_et: str | None = None
    entry_window_end_et: str | None = None
    shadow_only: bool = False
    # HIGH-1: the deployment's ACTUAL runtime mode (kernel ``RuntimeMode`` wire
    # value: ``advisory`` / ``shadow`` / ``live_approval_gated`` / ``live_automated``).
    # This is the real source the profile-exit dispatch gate consults — NOT a
    # hardcoded constant. DEFAULT ``None`` and ``None`` FAILS CLOSED: the fail-
    # closed dispatch allowlist (``profile_exit_dispatch_allowed``) only ever opens
    # for the lone permitted mode ``live_approval_gated``; any other value
    # (``live_automated``, ``shadow``, ``advisory``, an unknown string, or ``None``)
    # keeps the gate shut. A deployment actually running ``live_automated`` therefore
    # can NEVER dispatch a profile exit, which matches every other Bhiksha gate.
    runtime_mode: str | None = None

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
        _validate_optional_time_field(normalized, "entry_window_start_et")
        _validate_optional_time_field(normalized, "entry_window_end_et")
        return normalized


class RiskSpec(BaseModel):
    profile: str
    max_open_positions_total: int | None = None
    max_open_positions_per_symbol: int | None = None
    max_open_positions_per_deployment: int | None = None
    max_trade_premium_usd: float | None = None
    hard_flat_time_et: str | None = None
    stop_loss_pct: float = 0.45

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        _validate_optional_time_field(normalized, "hard_flat_time_et")
        return normalized


class ExitSpec(BaseModel):
    profile: str = "strategy_managed_v1"
    use_algorithmic_exit: bool = True
    use_profit_target: bool = False
    profit_target_multiple: float | None = None
    option_profit_target_pct: float | None = None
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

    # --- v2 operator exit-profile dials (premium-anchored, evaluated by
    # bhiksha.execution.profile_exit). All optional + back-compatible: the
    # defaults reproduce pre-v2 behavior (no staged targets, no partial, no
    # giveback, EOD flat on). Mirrors the kernel ManagementPolicySpec v2 fields.
    # When ``profile_exit_id`` is set the deployment carries a frozen named
    # operator exit profile; the evaluator runs it SHADOW-FIRST. ---
    profile_exit_id: str | None = None
    # Legacy shadow flag (pre-dates the live-monitor wiring). Retained for the
    # offline shadow-receipt tool. The LIVE-monitor dispatch gate is driven by
    # ``profile_exit_drives_live`` below (the single operator switch), not by this.
    profile_exit_shadow_only: bool = True
    # Operator live-enablement flag for the profile-exit evaluator. DEFAULT FALSE
    # and the ONLY state this wave ships: when False the profile decision is
    # RECORD-ONLY (shadow) and can never reach the broker/order path. Flipping it
    # to True (a deliberate later operator action) is the single switch that lets
    # a recorded profile decision DRIVE a real exit — and even then the
    # fail-closed dispatch allowlist (profile_exit_dispatch_allowed) still applies.
    # An env override (BHIKSHA_PROFILE_EXIT_LIVE) can force it on at runtime.
    profile_exit_drives_live: bool = False
    target_1_r: float | None = None
    target_2_r: float | None = None
    target_1_quantity: float = 1.0
    initial_stop_pct: float | None = None
    premium_disaster_stop_pct: float | None = None
    no_progress_seconds: int | None = None
    max_hold_seconds: int | None = None
    high_water_giveback_policy: str = "OFF"
    breakeven_after_t1: bool = True
    eod_flat: bool = True
    # L1: configurable favorable-excursion floor (in R) for the no-progress time
    # stop; the profile evaluator defaults to 0.25 when unset.
    no_progress_favorable_floor_r: float = 0.25

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        _validate_optional_time_field(normalized, "hard_flat_time_et")
        _validate_nested_optional_time_field(normalized, "catastrophe_exit_params", "hard_flat_time_et")
        thesis_exit_policy = normalized.get("thesis_exit_policy")
        if isinstance(thesis_exit_policy, str):
            normalized["thesis_exit_policy"] = thesis_exit_policy.strip() or None
        return normalized

    @model_validator(mode="after")
    def validate_profile_stop_ordering(self) -> "ExitSpec":
        # M2: reject an inverted profile stop config. The premium disaster stop is
        # a catastrophe backstop and must be at least as wide as the initial stop;
        # a tighter disaster stop would silently pre-empt the initial stop and
        # change the ladder's risk semantics. Fail loud at config time.
        initial = self.initial_stop_pct
        disaster = self.premium_disaster_stop_pct
        if (
            initial is not None
            and disaster is not None
            and initial > 0
            and disaster > 0
            and disaster < initial
        ):
            raise ValueError(
                "ExitSpec premium_disaster_stop_pct "
                f"({disaster}) must not be tighter than initial_stop_pct ({initial}); "
                "the disaster stop is a catastrophe backstop and must be wider (>=)"
            )
        return self


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

    @model_validator(mode="after")
    def validate_exit_safety(self) -> "DeploymentManifest":
        _validate_exit_safety(
            identifier=f"deployment {self.deployment_id!r}",
            strategy_key=self.strategy.key,
            exit_spec=self.exit,
        )
        _validate_profile_recovery_stop(
            identifier=f"deployment {self.deployment_id!r}",
            exit_spec=self.exit,
            risk_spec=self.risk,
        )
        return self


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
    # Carries the flat ``Operator_Defaults_v1`` "default"-section dict (see
    # ``load_operator_defaults_sheet_rows`` in ``bhiksha.active_plan.compiler``)
    # through into the compiled plan payload so the live runtime can read
    # operator-set risk knobs at session start without a new network
    # dependency (the plan is already synced at live-start). Additive,
    # harmless if absent: defaults to ``{}`` for any plan compiled before
    # this field existed. See ``bhiksha.risk.plan_operator_defaults_source``.
    operator_defaults: dict[str, Any] = Field(default_factory=dict)


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

    @model_validator(mode="after")
    def validate_exit_safety(self) -> "StrategyCatalogEntry":
        _validate_exit_safety(
            identifier=f"strategy catalog entry {self.strategy_id!r}",
            strategy_key=self.strategy.key,
            exit_spec=self.exit,
        )
        _validate_profile_recovery_stop(
            identifier=f"strategy catalog entry {self.strategy_id!r}",
            exit_spec=self.exit,
            risk_spec=self.risk,
        )
        return self


class VehicleProfile(BaseModel):
    profile: str
    long_signal_contract_type: str = "CALL"
    short_signal_contract_type: str = "PUT"
    dte_min: int = 0
    dte_max: int = 7
    dte_fallback_policy: Literal["strict", "allow_nearest_after"] = "strict"
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
