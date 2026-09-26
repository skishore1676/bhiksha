"""Strict operator-owned exit profiles. No synthetic catalog on a Sheet failure."""
from __future__ import annotations

from datetime import time
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ExitProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    exit_profile_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    trade_archetype: Literal["TREND_CONTINUATION", "FLASH_REVERSAL", "EXHAUSTION_REVERSAL", "RANGE_EXPANSION"]
    # Mechanics supported by the evaluators.
    exit_family: Literal["staged_r_ladder", "dynamic_envelope", "time_fuse", "profit_preservation_ratchet"]
    target_1_r: float = Field(gt=0)
    target_2_r: float = Field(gt=0)
    target_1_quantity: float = Field(gt=0, le=1)
    initial_stop_pct: float = Field(gt=0, lt=1)
    disaster_stop_pct: float = Field(gt=0, lt=1)
    stop_anchor: Literal["option_premium"] = "option_premium"
    structural_buffer: float | None = None
    structural_buffer_unit: str | None = None
    reference_timeframe: str | None = None
    no_progress_seconds: int | None = Field(default=None, gt=0)
    no_progress_min_r: float | None = Field(default=None, ge=0)
    profit_lock_arm_r: float | None = Field(default=None, gt=0)
    profit_lock_floor_r: float | None = Field(default=None, ge=0)
    max_hold_seconds: int | None = Field(default=None, gt=0)
    giveback_policy: Literal["OFF", "STRICT", "MODERATE", "LOOSE"]
    giveback_arm_r: float | None = Field(default=None, gt=0)
    giveback_retrace_fraction: float | None = Field(default=None, gt=0, lt=1)
    risk_envelope_enabled: bool = False
    risk_envelope_curvature: float | None = Field(default=None, gt=0)
    risk_envelope_activation_r: float | None = Field(default=None, ge=0)
    risk_envelope_initial_floor_r: float | None = None
    risk_envelope_floor_at_t1_r: float | None = None
    risk_envelope_ratchet_step_r: float | None = Field(default=None, gt=0)
    breakeven_after_t1: bool
    eod_flat: bool
    hard_flat_time_et: str
    description: str = ""

    @model_validator(mode="after")
    def validate_mechanics(self) -> "ExitProfileConfig":
        time.fromisoformat(self.hard_flat_time_et)
        if self.target_2_r < self.target_1_r:
            raise ValueError("target_2_r must be >= target_1_r")
        if self.disaster_stop_pct < self.initial_stop_pct:
            raise ValueError("disaster_stop_pct must be >= initial_stop_pct")
        if not self.eod_flat and self.trade_archetype != "RANGE_EXPANSION":
            raise ValueError("only range expansion supports overnight holding")
        if self.trade_archetype == "RANGE_EXPANSION" and not self.eod_flat:
            if self.max_hold_seconds is None:
                raise ValueError("overnight range expansion needs a maximum hold")
        elif self.no_progress_seconds is None:
            raise ValueError("intraday profiles need a no-progress timer")
        if self.giveback_policy != "OFF" and (self.giveback_arm_r is None or self.giveback_retrace_fraction is None):
            raise ValueError("giveback needs explicit arm and retrace values")
        if self.giveback_policy == "OFF" and (self.giveback_arm_r is not None or self.giveback_retrace_fraction is not None):
            raise ValueError("OFF giveback cannot contain active giveback parameters")
        if self.risk_envelope_enabled != (self.exit_family == "dynamic_envelope"):
            raise ValueError("exit_family and risk_envelope_enabled must agree")
        if self.risk_envelope_enabled and self.risk_envelope_curvature is None:
            raise ValueError("dynamic envelope needs explicit curvature")
        if not self.risk_envelope_enabled and self.risk_envelope_curvature is not None:
            raise ValueError("inactive envelope cannot contain curvature")
        envelope_values = (self.risk_envelope_activation_r, self.risk_envelope_initial_floor_r,
                           self.risk_envelope_curvature, self.risk_envelope_floor_at_t1_r,
                           self.risk_envelope_ratchet_step_r)
        if self.risk_envelope_enabled and any(v is None for v in envelope_values):
            raise ValueError("dynamic envelope requires every envelope field")
        if not self.risk_envelope_enabled and any(v is not None for v in envelope_values):
            raise ValueError("inactive envelope cannot carry envelope fields")
        if any(v is not None for v in (self.structural_buffer, self.structural_buffer_unit, self.reference_timeframe)):
            raise ValueError("structural stops require underlying-bar execution support")
        lock = (self.profit_lock_arm_r, self.profit_lock_floor_r)
        if self.exit_family == "profit_preservation_ratchet":
            if any(v is None for v in lock) or lock[1] >= lock[0]:
                raise ValueError("profit preservation requires explicit floor < arm")
        elif any(v is not None for v in lock):
            raise ValueError("profit lock fields require profit_preservation_ratchet")
        if self.risk_envelope_enabled:
            from bhiksha.execution.exit_policy import evaluate_risk_envelope
            evaluate_risk_envelope(peak_r=0, activation_r=self.risk_envelope_activation_r,
                target_1_r=self.target_1_r, initial_floor_r=self.risk_envelope_initial_floor_r,
                floor_at_t1_r=self.risk_envelope_floor_at_t1_r, curvature=self.risk_envelope_curvature)
        return self

    def to_management_policy_spec_dict(self) -> dict[str, Any]:
        """Convert into canonical kernel ManagementPolicySpec dictionary."""
        payload: dict[str, Any] = {
            "policy_id": self.exit_profile_id,
            "policy_schema_version": "exit-policy.v1",
            "stop_family": "premium_pct",
            "stop_anchor": self.stop_anchor or "option_premium",
            "exit_family": "staged_r",
            "target_model": "staged_r",
            "target_r": float(self.target_2_r),
            "target_1_r": float(self.target_1_r),
            "target_2_r": float(self.target_2_r),
            "target_1_quantity": float(self.target_1_quantity),
            "initial_stop_pct": float(self.initial_stop_pct),
            "premium_disaster_stop_pct": float(self.disaster_stop_pct),
            "no_progress_seconds": int(self.no_progress_seconds) if self.no_progress_seconds is not None else None,
            "max_hold_seconds": int(self.max_hold_seconds) if self.max_hold_seconds is not None else None,
            "high_water_giveback_policy": str(self.giveback_policy).upper(),
            "giveback_arm_r": float(self.giveback_arm_r) if self.giveback_arm_r is not None else None,
            "giveback_retrace_fraction": (
                float(self.giveback_retrace_fraction) if self.giveback_retrace_fraction is not None else None
            ),
            "risk_envelope_enabled": bool(self.risk_envelope_enabled),
            "risk_envelope_curvature": (
                float(self.risk_envelope_curvature)
                if (self.risk_envelope_enabled and self.risk_envelope_curvature is not None)
                else None
            ),
            "breakeven_after_t1": bool(self.breakeven_after_t1),
            "eod_flat": bool(self.eod_flat),
            "hard_flat_time_et": str(self.hard_flat_time_et) if self.hard_flat_time_et else None,
            "option_stop_fallback_pct": float(self.disaster_stop_pct),
            "parameters": {
                "named_exit_evaluator_version": "named-exits.v2",
                "trade_archetype": self.trade_archetype,
                "exit_family": self.exit_family,
                "description": self.description,
            },
        }
        if self.profit_lock_arm_r is not None:
            payload["parameters"].update(profit_lock_arm_r=self.profit_lock_arm_r,
                                         profit_lock_floor_r=self.profit_lock_floor_r)
        if self.no_progress_min_r is not None:
            payload["parameters"]["no_progress_favorable_floor_r"] = float(self.no_progress_min_r)
        if self.structural_buffer is not None:
            payload["parameters"]["structural_buffer"] = float(self.structural_buffer)
        if self.structural_buffer_unit:
            payload["parameters"]["structural_buffer_unit"] = str(self.structural_buffer_unit)
        if self.reference_timeframe:
            payload["parameters"]["reference_timeframe"] = str(self.reference_timeframe)
        for key in ("risk_envelope_activation_r", "risk_envelope_initial_floor_r",
                    "risk_envelope_floor_at_t1_r", "risk_envelope_ratchet_step_r"):
            payload[key] = getattr(self, key)
        return payload


def load_exit_profiles_sheet_rows(rows: list[dict[str, Any]]) -> dict[str, ExitProfileConfig]:
    catalog: dict[str, ExitProfileConfig] = {}
    for index, raw in enumerate(rows, start=2):
        values = {str(k).strip(): v for k, v in raw.items() if v is not None and str(v).strip() != ""}
        # Reader injects row_index for tracking; remove it before strict model validation
        values.pop("row_index", None)
        if not values or str(values.get("exit_profile_id", "")).startswith("#"):
            continue
        # Normalize Sheet strings; invalid values are errors, never defaults.
        for key in ("exit_profile_id", "exit_family"):
            if key in values:
                values[key] = str(values[key]).strip().lower()
        for key in ("trade_archetype", "giveback_policy"):
            if key in values:
                values[key] = str(values[key]).strip().upper()

        # Handle Sheet column aliases and unit conversions
        if "target_1_fraction" in values and "target_1_quantity" not in values:
            values["target_1_quantity"] = values.pop("target_1_fraction")
        if "no_progress_minutes" in values and "no_progress_seconds" not in values:
            raw_npm = values.pop("no_progress_minutes")
            if raw_npm is not None and str(raw_npm).strip():
                values["no_progress_seconds"] = int(float(raw_npm) * 60)
        if "max_hold_minutes" in values and "max_hold_seconds" not in values:
            raw_mhm = values.pop("max_hold_minutes")
            if raw_mhm is not None and str(raw_mhm).strip():
                values["max_hold_seconds"] = int(float(raw_mhm) * 60)

        for key in ("eod_flat", "risk_envelope_enabled", "breakeven_after_t1"):
            if key in values and isinstance(values[key], str):
                value = values[key].strip().lower()
                if value not in {"true", "false"}:
                    raise ValueError(f"Exit_Profiles_v1 row {index}: {key} must be true or false")
                values[key] = value == "true"

        try:
            profile = ExitProfileConfig.model_validate(values)
        except ValueError as exc:
            raise ValueError(f"Exit_Profiles_v1 row {index}: {exc}") from exc
        if profile.exit_profile_id in catalog:
            raise ValueError(f"Exit_Profiles_v1 row {index}: duplicate profile {profile.exit_profile_id}")
        catalog[profile.exit_profile_id] = profile
    return catalog
