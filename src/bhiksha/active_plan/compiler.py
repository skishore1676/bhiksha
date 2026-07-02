"""Compile a sheet-style operator plan into Bhiksha's active plan contract."""

from __future__ import annotations

import csv
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import yaml

from bhiksha.config.loader import load_strategy_catalog
from bhiksha.config.models import ActivePlan, DeploymentManifest, StrategyCatalogEntry
from bhiksha.integrations.google_sheets import GoogleSheetTableClient
from bhiksha.strategy.capabilities import (
    NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS,
    derive_strategy_variant,
    evaluate_strategy_capability,
)
from bhiksha.strategy.registry import default_strategy_registry
from bhiksha.time_utils import normalize_time_text, parse_time_text


class StrategyCatalogSheetRow(BaseModel):
    """Read-only metadata row loaded from a Mala catalog/evidence tab."""

    model_config = ConfigDict(extra="ignore")

    catalog_key: str
    playbook_id: str | None = None
    symbol: str | None = None
    bias_template: str | None = None
    strategy_key: str | None = None
    strategy_family: str | None = None
    direction: str | None = None
    lifecycle_status: str | None = None
    operator_status_override: str | None = None
    operator_notes: str | None = None
    bhiksha_ready: bool = False
    first_validated_date: str | None = None
    last_validated_date: str | None = None
    validation_count: int | None = None
    expectancy: float | None = None
    confidence: float | None = None
    signal_count: int | None = None
    execution_robustness: float | None = None
    thesis_exit_policy: str | None = None
    playbook_summary_json: dict[str, Any] | list[Any] | str | None = None
    mala_handoff_version: int | None = None
    hypothesis_id: str | None = None
    strategy_name: str | None = None
    strategy_variant: str | None = None
    strategy_params_json: dict[str, Any] | list[Any] | str | None = None
    signal_window_et: str | None = None
    signal_window_derivation: str | None = None
    recommendation_tier: str | None = None
    recommendation_tier_reason: str | None = None
    recommendation_checks_json: dict[str, Any] | list[Any] | str | None = None
    m5_execution_profile: str | None = None
    m5_stress_profile: str | None = None
    thesis_exit_tested: bool | None = None
    thesis_exit_params_json: dict[str, Any] | list[Any] | str | None = None
    thesis_exit_metrics_json: dict[str, Any] | list[Any] | str | None = None
    option_trade_ready: bool | None = None
    option_adjusted_expectancy_pct: float | None = None
    option_exit_quality: str | None = None
    recommended_dte_min: int | None = None
    recommended_dte_max: int | None = None
    theta_penalty_pct: float | None = None
    expectancy_pct: float | None = None
    avg_win_pct: float | None = None
    avg_loss_pct_abs: float | None = None
    pnl_pct_per_minute: float | None = None
    pnl_pct_per_bar: float | None = None
    median_minutes_held: float | None = None
    avg_minutes_held: float | None = None
    target_hit_rate: float | None = None
    stop_loss_rate: float | None = None
    target_hit_within_15_minutes: float | None = None
    target_hit_within_30_minutes: float | None = None
    stop_loss_within_15_minutes: float | None = None
    exit_reliability: str | None = None
    exit_trade_count: int | None = None
    bhiksha_capability_status: str | None = None
    bhiksha_capability_reason: str | None = None
    provider_validation_status: str | None = None
    provider_feature_risk: str | None = None
    provider_signal_overlap: float | None = None
    provider_validation_report: str | None = None
    bhiksha_runtime_supported: bool | None = None
    bhiksha_runtime_reason: str | None = None
    mala_evidence_ready: bool | None = None
    mala_evidence_blocking_checks: str | None = None
    activation_candidate: bool | None = None
    activation_blocking_checks: str | None = None
    m7_status: str | None = None
    m7_feature_risk: str | None = None
    m7_signal_overlap: float | None = None
    triage_verdict: str | None = None
    triage_verdict_reason: str | None = None
    triage_blocking_checks: str | None = None
    triage_advisory_notes: str | None = None
    triage_artifact: str | None = None
    warnings: str | None = None
    exit_profile_spec: dict[str, Any] | None = None
    row_index: int | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = _normalize_sheet_mapping(data)
        if normalized.get("mala_handoff_version") is not None:
            normalized = _normalize_mala_evidence_row(normalized)
        if normalized.get("symbol") is not None:
            normalized["symbol"] = str(normalized["symbol"]).strip().upper()
        for key in (
            "strategy_key",
            "strategy_family",
            "direction",
            "lifecycle_status",
            "operator_status_override",
            "recommendation_tier",
            "exit_reliability",
        ):
            if normalized.get(key) is not None:
                normalized[key] = str(normalized[key]).strip().lower()
        return normalized

    @model_validator(mode="after")
    def validate_row(self) -> "StrategyCatalogSheetRow":
        self.catalog_key = self.catalog_key.strip()
        if not self.catalog_key:
            raise ValueError("catalog_key is required")
        if self.symbol is not None:
            self.symbol = self.symbol.upper()
        return self


class ActivePlanSheetRow(BaseModel):
    """Canonical row schema for the operator control-plane sheet."""

    model_config = ConfigDict(extra="ignore")

    row_id: str
    row_type: Literal["strategy", "manual"]
    enabled: bool = True
    symbol: str | None = None
    authorization_mode: Literal["shadow", "live"] = "shadow"
    strategy_id: str | None = None
    manual_setup_type: str | None = None
    direction: Literal["long", "short"] | None = None
    trigger_price: float | None = None
    trigger_direction: Literal["ABOVE", "BELOW", "CLOSE_BY"] | None = None
    after_time_et: str | None = None
    close_by_factor: float | None = None
    end_in_days: int | None = None
    max_trade_premium_usd: float | None = None
    stop_loss_pct: float | None = None
    hard_flat_time_et: str | None = None
    use_profit_target: bool | None = None
    profit_target_multiple: float | None = None
    option_profit_target_pct: float | None = None
    stop_to_breakeven_after_r_multiple: float | None = None
    entry_window_start_et: str | None = None
    entry_window_end_et: str | None = None
    notes: str | None = None
    strategy_params_override: dict[str, Any] = Field(default_factory=dict)
    execution_overrides: dict[str, Any] = Field(default_factory=dict)
    risk_overrides: dict[str, Any] = Field(default_factory=dict)
    exit_overrides: dict[str, Any] = Field(default_factory=dict)
    # Single JSON cell carrying the kernel ``ManagementPolicySpec`` (model_dump)
    # that mala publishes for an assigned exit profile. Sheet column name:
    # ``management_policy_spec`` (alias ``exit_profile_spec``). When present the
    # compiler maps its v2 fields onto the deployment ``ExitSpec`` so the live
    # profile-exit evaluator sees the operator's named exit DNA. ``None`` (the
    # default) reproduces pre-bridge behavior exactly: no v2 fields are set.
    exit_profile_spec: dict[str, Any] | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = _normalize_sheet_mapping(data)

        authorization = normalized.get("authorization") or normalized.get("authorization_mode")
        if authorization is not None:
            authorization_value = str(authorization).strip().lower()
            if authorization_value in {"paper", "dry_run", "dryrun"}:
                authorization_value = "shadow"
            normalized["authorization_mode"] = authorization_value

        row_type = normalized.get("row_type")
        if row_type is not None:
            row_type_value = str(row_type).strip().lower()
            if row_type_value == "manual_trigger":
                normalized["row_type"] = "manual"
                normalized.setdefault("manual_setup_type", "manual_trigger")
            elif row_type_value in {"breakout", "manual_breakout"}:
                normalized["row_type"] = "manual"
                normalized.setdefault("manual_setup_type", "breakout")
            else:
                normalized["row_type"] = row_type_value

        manual_setup_type = normalized.get("manual_setup_type")
        if manual_setup_type is not None:
            normalized["manual_setup_type"] = str(manual_setup_type).strip().lower()
        if normalized.get("row_type") == "manual" and normalized.get("manual_setup_type") is None and normalized.get("strategy_id"):
            normalized["manual_setup_type"] = str(normalized["strategy_id"]).strip().lower()
        if normalized.get("symbol") is not None:
            normalized["symbol"] = str(normalized["symbol"]).strip().upper()
        if normalized.get("direction") is not None:
            normalized["direction"] = str(normalized["direction"]).strip().lower()
        if normalized.get("trigger_direction") is not None:
            normalized["trigger_direction"] = _normalize_trigger_direction(str(normalized["trigger_direction"]))
        for key in (
            "strategy_params_override",
            "execution_overrides",
            "risk_overrides",
            "exit_overrides",
            "source_metadata",
        ):
            if normalized.get(key) is None:
                normalized[key] = {}
        for key, metadata_key in (
            ("entry_window_start_et", "start_date"),
            ("entry_window_end_et", "end_date"),
        ):
            if _looks_like_date(normalized.get(key)):
                normalized["source_metadata"] = {
                    **(normalized.get("source_metadata") or {}),
                    metadata_key: normalized[key],
                }
                normalized[key] = None
        return normalized

    @model_validator(mode="after")
    def validate_row(self) -> "ActivePlanSheetRow":
        self.row_id = self.row_id.strip()
        if not self.row_id:
            raise ValueError("row_id is required")

        if self.row_type == "strategy":
            if not self.strategy_id:
                raise ValueError("strategy rows require strategy_id")
        else:
            manual_setup = (self.manual_setup_type or "").strip().lower()
            if manual_setup not in {"manual_trigger", "trigger", "breakout", "manual_breakout"}:
                raise ValueError("manual rows currently support manual_setup_type=manual_trigger or breakout")
            if not self.symbol:
                raise ValueError("manual rows require symbol")
            self.symbol = self.symbol.upper()
            if self.direction is None:
                raise ValueError("manual rows require direction")
            if self.trigger_price is None:
                raise ValueError("manual rows require trigger_price")
            if self.trigger_direction is None:
                raise ValueError("manual rows require trigger_direction")
            if self.after_time_et is not None:
                normalized_after = normalize_time_text(str(self.after_time_et))
                if normalized_after is None:
                    self.after_time_et = None
                else:
                    parse_time_text(normalized_after)
                    self.after_time_et = normalized_after
        return self


@dataclass(slots=True, frozen=True)
class CompiledActivePlan:
    plan: ActivePlan
    rows: list[ActivePlanSheetRow]


@dataclass(slots=True, frozen=True)
class RowValidationResult:
    rows: list[ActivePlanSheetRow]
    suppressed: list[dict[str, Any]]


@dataclass(slots=True, frozen=True)
class StrategyCatalogValidationResult:
    rows: list[StrategyCatalogSheetRow]
    suppressed: list[dict[str, Any]]


def compile_active_plan_from_sheet(
    *,
    sheet_path: str | Path,
    strategy_catalog_path: str | Path,
    active_plan_id: str | None = None,
    trading_date: str | None = None,
    source_name: str = "google_sheet_integration",
) -> CompiledActivePlan:
    validation = load_sheet_rows_with_report(sheet_path)
    return compile_active_plan_from_rows(
        rows=validation.rows,
        strategy_catalog_path=strategy_catalog_path,
        active_plan_id=active_plan_id,
        trading_date=trading_date,
        source_name=source_name,
        source_details={"sheet_path": str(Path(sheet_path).resolve())},
        suppressed=list(validation.suppressed),
    )


def compile_active_plan_from_rows(
    *,
    rows: list[ActivePlanSheetRow],
    strategy_catalog_path: str | Path,
    active_plan_id: str | None = None,
    trading_date: str | None = None,
    source_name: str = "google_sheet_integration",
    source_details: dict[str, Any] | None = None,
    google_strategy_catalog: list[StrategyCatalogSheetRow] | None = None,
    operator_defaults: dict[str, Any] | None = None,
    suppressed: list[dict[str, Any]] | None = None,
) -> CompiledActivePlan:
    suppressed_rows = list(suppressed or [])
    if google_strategy_catalog is not None:
        sync_google_strategy_catalog(
            strategy_catalog_path=strategy_catalog_path,
            google_strategy_catalog=google_strategy_catalog,
            operator_defaults=operator_defaults or {},
        )
    strategy_catalog = load_strategy_catalog(strategy_catalog_path)
    catalog_by_id = {entry.strategy_id: entry for entry in strategy_catalog}
    google_catalog_by_id = {
        entry.catalog_key: entry
        for entry in (google_strategy_catalog or [])
    }
    enforce_google_catalog = google_strategy_catalog is not None

    deployments: list[DeploymentManifest] = []
    row_type_counts: dict[str, int] = {}
    seen_row_ids: set[str] = set()
    for row in rows:
        if row.row_id in seen_row_ids:
            suppressed_rows.append(
                _suppressed_row(
                    reason=f"Duplicate row_id in active-plan sheet: {row.row_id}",
                    row=row,
                )
            )
            continue
        seen_row_ids.add(row.row_id)
        try:
            deployment = _compile_row(row, catalog_by_id, google_catalog_by_id, enforce_google_catalog)
        except (ValidationError, ValueError) as exc:
            suppressed_rows.append(_suppressed_row(reason=str(exc), row=row))
            continue
        deployments.append(deployment)
        row_type_counts[row.row_type] = row_type_counts.get(row.row_type, 0) + 1

    effective_trading_date = trading_date or datetime.now(UTC).date().isoformat()
    effective_active_plan_id = active_plan_id or f"active_plan_{effective_trading_date}"
    source = {
        "name": source_name,
        "strategy_catalog_path": str(Path(strategy_catalog_path).resolve()),
    }
    if source_details:
        source.update(source_details)
    plan = ActivePlan(
        active_plan_id=effective_active_plan_id,
        trading_date=effective_trading_date,
        generated_at=datetime.now(UTC).isoformat(),
        source=source,
        summary={
            "row_count": len(rows),
            "row_type_counts": row_type_counts,
            "google_strategy_catalog_count": len(google_catalog_by_id),
            "deployment_count": len(deployments),
            "enabled_deployment_count": sum(1 for deployment in deployments if deployment.enabled),
            "suppressed_count": len(suppressed_rows),
            "symbols": sorted({deployment.symbol for deployment in deployments}),
        },
        suppressed=suppressed_rows,
        deployments=deployments,
    )
    return CompiledActivePlan(plan=plan, rows=rows)


def compile_active_plan_from_google_sheets(
    *,
    spreadsheet_id: str,
    credentials_path: str | Path,
    strategy_sheet_name: str,
    manual_sheet_name: str,
    strategy_catalog_path: str | Path,
    active_plan_id: str | None = None,
    trading_date: str | None = None,
    source_name: str = "google_sheets_control_plane",
    catalog_sheet_name: str = "strategy catalog",
    defaults_sheet_name: str | None = None,
    strategy_client: GoogleSheetTableClient | None = None,
    manual_client: GoogleSheetTableClient | None = None,
    catalog_client: GoogleSheetTableClient | None = None,
    defaults_client: GoogleSheetTableClient | None = None,
) -> CompiledActivePlan:
    if catalog_client is None:
        catalog_client = GoogleSheetTableClient(
            spreadsheet_id=spreadsheet_id,
            sheet_name=catalog_sheet_name,
            credentials_path=Path(credentials_path),
        )
    if strategy_client is None:
        strategy_client = GoogleSheetTableClient(
            spreadsheet_id=spreadsheet_id,
            sheet_name=strategy_sheet_name,
            credentials_path=Path(credentials_path),
        )
    if manual_client is None:
        manual_client = GoogleSheetTableClient(
            spreadsheet_id=spreadsheet_id,
            sheet_name=manual_sheet_name,
            credentials_path=Path(credentials_path),
        )
    if defaults_client is None and defaults_sheet_name:
        defaults_client = GoogleSheetTableClient(
            spreadsheet_id=spreadsheet_id,
            sheet_name=defaults_sheet_name,
            credentials_path=Path(credentials_path),
        )

    catalog_validation = load_strategy_catalog_sheet_rows_with_report(
        catalog_client.read_rows(),
        sheet_name=catalog_client.sheet_name,
    )
    operator_defaults = (
        load_operator_defaults_sheet_rows(defaults_client.read_rows())
        if defaults_client is not None
        else {}
    )
    strategy_validation = load_rows_from_sheet_records_with_report(
        strategy_client.read_rows(),
        row_type="strategy",
        sheet_name=strategy_client.sheet_name,
    )
    manual_validation = load_rows_from_sheet_records_with_report(
        manual_client.read_rows(),
        row_type="manual",
        sheet_name=manual_client.sheet_name,
    )
    suppressed = [
        *catalog_validation.suppressed,
        *strategy_validation.suppressed,
        *manual_validation.suppressed,
    ]
    return compile_active_plan_from_rows(
        rows=[*strategy_validation.rows, *manual_validation.rows],
        strategy_catalog_path=strategy_catalog_path,
        active_plan_id=active_plan_id,
        trading_date=trading_date,
        source_name=source_name,
        source_details={
            "spreadsheet_id": catalog_client.spreadsheet_id,
            "catalog_sheet_name": catalog_client.sheet_name,
            "defaults_sheet_name": defaults_client.sheet_name if defaults_client is not None else None,
            "strategy_sheet_name": strategy_client.sheet_name,
            "manual_sheet_name": manual_client.sheet_name,
        },
        google_strategy_catalog=catalog_validation.rows,
        operator_defaults=operator_defaults,
        suppressed=suppressed,
    )


def write_compiled_active_plan(
    *,
    sheet_path: str | Path,
    strategy_catalog_path: str | Path,
    output_path: str | Path,
    active_plan_id: str | None = None,
    trading_date: str | None = None,
    source_name: str = "google_sheet_integration",
) -> ActivePlan:
    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=strategy_catalog_path,
        active_plan_id=active_plan_id,
        trading_date=trading_date,
        source_name=source_name,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(compiled.plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return compiled.plan


def load_sheet_rows(path: str | Path) -> list[ActivePlanSheetRow]:
    return load_sheet_rows_with_report(path).rows


def load_sheet_rows_with_report(path: str | Path) -> RowValidationResult:
    resolved = Path(path)
    if resolved.suffix.lower() == ".csv":
        return _load_csv_rows_with_report(resolved)
    if resolved.suffix.lower() == ".json":
        return _load_json_rows_with_report(resolved)
    raise ValueError(f"Unsupported sheet export format: {resolved.suffix}")


def load_rows_from_sheet_records(
    rows: list[dict[str, Any]],
    *,
    row_type: Literal["strategy", "manual"],
) -> list[ActivePlanSheetRow]:
    return load_rows_from_sheet_records_with_report(rows, row_type=row_type).rows


def load_rows_from_sheet_records_with_report(
    rows: list[dict[str, Any]],
    *,
    row_type: Literal["strategy", "manual"],
    sheet_name: str | None = None,
) -> RowValidationResult:
    normalized_rows: list[ActivePlanSheetRow] = []
    suppressed: list[dict[str, Any]] = []
    for row in rows:
        payload = _prepare_sheet_row_payload(row, row_type=row_type, sheet_name=sheet_name)
        if _should_skip_prepared_row(payload):
            continue
        try:
            normalized_rows.append(ActivePlanSheetRow.model_validate(payload))
        except (ValidationError, ValueError) as exc:
            suppressed.append(_suppressed_row(reason=str(exc), payload=payload, raw=row))
    return RowValidationResult(rows=normalized_rows, suppressed=suppressed)


def load_strategy_catalog_sheet_rows(rows: list[dict[str, Any]]) -> list[StrategyCatalogSheetRow]:
    return load_strategy_catalog_sheet_rows_with_report(rows).rows


def load_operator_defaults_sheet_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for row in rows:
        section = str(row.get("section") or "default").strip() or "default"
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        value = _normalize_value(row.get("value"))
        if section == "default":
            defaults[key] = value
            continue
        nested = defaults.setdefault(section, {})
        if isinstance(nested, dict):
            nested[key] = value
    return defaults


def load_strategy_catalog_sheet_rows_with_report(
    rows: list[dict[str, Any]],
    *,
    sheet_name: str | None = None,
) -> StrategyCatalogValidationResult:
    validated_rows: list[StrategyCatalogSheetRow] = []
    suppressed: list[dict[str, Any]] = []
    for row in rows:
        payload = _prepare_sheet_row_payload(row, sheet_name=sheet_name)
        try:
            validated_rows.append(StrategyCatalogSheetRow.model_validate(payload))
        except (ValidationError, ValueError) as exc:
            suppressed.append(_suppressed_row(reason=str(exc), payload=payload, raw=row, row_type="catalog"))
    return StrategyCatalogValidationResult(rows=validated_rows, suppressed=suppressed)


def sync_google_strategy_catalog(
    *,
    strategy_catalog_path: str | Path,
    google_strategy_catalog: list[StrategyCatalogSheetRow],
    operator_defaults: dict[str, Any] | None = None,
) -> list[Path]:
    catalog_root = Path(strategy_catalog_path)
    generated_root = catalog_root / "google_promoted"
    generated_root.mkdir(parents=True, exist_ok=True)

    supported_keys = set(default_strategy_registry()._strategies)
    preserved_strategy_ids = _manual_strategy_catalog_ids(catalog_root, generated_root)
    eligible_entries = [
        entry
        for entry in google_strategy_catalog
        if entry.catalog_key not in preserved_strategy_ids and _is_google_catalog_entry_promotable(entry, supported_keys)
    ]

    expected_paths = {generated_root / f"{entry.catalog_key}.yaml" for entry in eligible_entries}
    written_paths: list[Path] = []
    for entry in eligible_entries:
        output_path = generated_root / f"{entry.catalog_key}.yaml"
        _atomic_yaml_write(
            output_path,
            yaml.safe_dump(
                _google_catalog_entry_payload(entry, operator_defaults=operator_defaults or {}),
                sort_keys=False,
            ),
        )
        written_paths.append(output_path)
    for stale_file in sorted(generated_root.rglob("*.yaml")):
        if stale_file not in expected_paths:
            stale_file.unlink()
    return written_paths


def _load_csv_rows(path: Path) -> list[ActivePlanSheetRow]:
    return _load_csv_rows_with_report(path).rows


def _load_csv_rows_with_report(path: Path) -> RowValidationResult:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        validated_rows: list[ActivePlanSheetRow] = []
        suppressed: list[dict[str, Any]] = []
        for index, row in enumerate(reader, start=2):
            if not any(str(value).strip() for value in row.values() if value is not None):
                continue
            payload = _prepare_sheet_row_payload(row, row_index=index, sheet_name=path.name)
            if _should_skip_prepared_row(payload):
                continue
            try:
                validated_rows.append(ActivePlanSheetRow.model_validate(payload))
            except (ValidationError, ValueError) as exc:
                suppressed.append(_suppressed_row(reason=str(exc), payload=payload, raw=row))
        return RowValidationResult(rows=validated_rows, suppressed=suppressed)


def _load_json_rows(path: Path) -> list[ActivePlanSheetRow]:
    return _load_json_rows_with_report(path).rows


def _load_json_rows_with_report(path: Path) -> RowValidationResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Active-plan sheet JSON export must be a list of row objects")
    validated_rows: list[ActivePlanSheetRow] = []
    suppressed: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            suppressed.append(
                {
                    "reason": "JSON sheet rows must be objects",
                    "row_index": index,
                    "sheet_name": path.name,
                    "row_type": "unknown",
                    "action": "suppressed",
                }
            )
            continue
        prepared = _prepare_sheet_row_payload(item, row_index=index, sheet_name=path.name)
        if _should_skip_prepared_row(prepared):
            continue
        try:
            validated_rows.append(ActivePlanSheetRow.model_validate(prepared))
        except (ValidationError, ValueError) as exc:
            suppressed.append(_suppressed_row(reason=str(exc), payload=prepared, raw=item))
    return RowValidationResult(rows=validated_rows, suppressed=suppressed)


def _compile_row(
    row: ActivePlanSheetRow,
    catalog_by_id: dict[str, StrategyCatalogEntry],
    google_catalog_by_id: dict[str, StrategyCatalogSheetRow] | None = None,
    enforce_google_catalog: bool = False,
) -> DeploymentManifest:
    if row.row_type == "strategy":
        return _compile_strategy_row(row, catalog_by_id, google_catalog_by_id or {}, enforce_google_catalog)
    manual_setup_type = _normalized_manual_setup_type(row.manual_setup_type)
    if manual_setup_type == "manual_trigger":
        return _compile_manual_trigger_row(row)
    return _compile_manual_breakout_row(row)


def _compile_strategy_row(
    row: ActivePlanSheetRow,
    catalog_by_id: dict[str, StrategyCatalogEntry],
    google_catalog_by_id: dict[str, StrategyCatalogSheetRow],
    enforce_google_catalog: bool,
) -> DeploymentManifest:
    strategy_id = row.strategy_id or ""
    google_catalog_entry = google_catalog_by_id.get(strategy_id)
    if enforce_google_catalog and google_catalog_entry is None:
        raise ValueError(f"Strategy {strategy_id!r} is not present in Google strategy catalog")
    if google_catalog_entry is not None:
        _validate_google_catalog_exit_contract(strategy_id, google_catalog_entry)

    entry = catalog_by_id.get(strategy_id)
    if entry is None:
        available = ", ".join(sorted(catalog_by_id))
        raise ValueError(
            f"Unknown strategy_id in active-plan sheet: {strategy_id}. "
            f"Promote this strategy into config/strategy_catalog first or remove it from active_strategy. "
            f"Available local strategies: {available}"
        )
    if not entry.enabled:
        raise ValueError(f"Strategy catalog entry {strategy_id!r} is disabled")
    if entry.approval_status != "approved":
        raise ValueError(f"Strategy catalog entry {strategy_id!r} is not approved")
    if row.symbol and row.symbol.upper() != entry.symbol:
        raise ValueError(
            f"Strategy row {row.row_id!r} overrides symbol {row.symbol!r}, "
            f"but catalog entry {strategy_id!r} is bound to {entry.symbol!r}"
        )
    relaxed_evidence_gates: list[str] = []
    if google_catalog_entry is not None:
        relaxed_evidence_gates = _validate_google_catalog_alignment(
            strategy_id,
            entry,
            google_catalog_entry,
            live=row.authorization_mode == "live",
        )
    if relaxed_evidence_gates and row.authorization_mode == "live":  # pragma: no cover - defense in depth
        raise ValueError(f"Strategy {strategy_id!r}: evidence gates may not be relaxed for a live row")
    effective_row = row
    if row.exit_profile_spec is None and google_catalog_entry is not None and google_catalog_entry.exit_profile_spec:
        effective_row = row.model_copy(update={"exit_profile_spec": google_catalog_entry.exit_profile_spec})

    payload = _catalog_entry_payload(entry)
    payload["deployment_id"] = effective_row.row_id
    payload["enabled"] = effective_row.enabled
    payload["execution"] = _apply_execution_overrides(payload["execution"], effective_row)
    payload["risk"] = _apply_risk_overrides(payload["risk"], effective_row)
    payload["exit"] = _apply_exit_overrides(payload["exit"], effective_row)
    payload["strategy"]["params"] = _deep_merge(payload["strategy"]["params"], effective_row.strategy_params_override)
    payload["source"] = _merge_source_metadata(
        payload["source"],
        row=effective_row,
        origin="active_sheet_strategy",
        extra_metadata={
            "strategy_id": strategy_id,
            "catalog_symbol": entry.symbol,
            **_google_catalog_metadata(google_catalog_entry),
            **({"evidence_gates_relaxed": relaxed_evidence_gates} if relaxed_evidence_gates else {}),
        },
    )
    return DeploymentManifest.model_validate(payload)


def _compile_manual_trigger_row(row: ActivePlanSheetRow) -> DeploymentManifest:
    stop_loss_pct = row.stop_loss_pct if row.stop_loss_pct is not None else 0.45
    hard_flat_time_et = row.hard_flat_time_et or "15:55"
    use_profit_target = (
        row.use_profit_target
        if row.use_profit_target is not None
        else row.profit_target_multiple is not None or row.option_profit_target_pct is not None
    )

    payload: dict[str, Any] = {
        "deployment_id": row.row_id,
        "enabled": row.enabled,
        "symbol": row.symbol,
        "strategy": {
            "key": "manual_trigger",
            "version": 1,
            "params": {
                "direction": row.direction,
                "trigger_price": row.trigger_price,
                "trigger_direction": row.trigger_direction,
            },
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": row.authorization_mode != "live",
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": row.end_in_days if row.end_in_days is not None else 2,
            "target_abs_delta_min": 0.45,
            "target_abs_delta_max": 0.60,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.20,
        },
        "risk": {
            "profile": "manual_trigger_v1",
            "max_trade_premium_usd": row.max_trade_premium_usd if row.max_trade_premium_usd is not None else 300.0,
            "hard_flat_time_et": hard_flat_time_et,
            "stop_loss_pct": stop_loss_pct,
        },
        "exit": {
            "profile": "manual_trigger_exit_v1",
            "use_algorithmic_exit": False,
            "use_profit_target": use_profit_target,
            "profit_target_multiple": row.profit_target_multiple,
            "option_profit_target_pct": row.option_profit_target_pct,
            "stop_loss_pct": stop_loss_pct,
            "stop_to_breakeven_after_r_multiple": row.stop_to_breakeven_after_r_multiple,
            "hard_flat_time_et": hard_flat_time_et,
        },
        "source": {
            "origin": "active_sheet_manual",
            "metadata": {
                "manual_setup_type": _normalized_manual_setup_type(row.manual_setup_type),
            },
        },
    }
    if row.after_time_et is not None:
        payload["strategy"]["params"]["after_time_et"] = row.after_time_et
    if row.close_by_factor is not None:
        payload["strategy"]["params"]["close_by_factor"] = row.close_by_factor

    payload["execution"] = _apply_execution_overrides(payload["execution"], row)
    payload["risk"] = _apply_risk_overrides(payload["risk"], row)
    payload["exit"] = _apply_exit_overrides(payload["exit"], row)
    payload["source"] = _merge_source_metadata(payload["source"], row=row, origin="active_sheet_manual")
    return DeploymentManifest.model_validate(payload)


def _compile_manual_breakout_row(row: ActivePlanSheetRow) -> DeploymentManifest:
    stop_loss_pct = row.stop_loss_pct if row.stop_loss_pct is not None else 0.35
    hard_flat_time_et = row.hard_flat_time_et or "15:53"
    profit_target_multiple = row.profit_target_multiple if row.profit_target_multiple is not None else 1.25
    use_profit_target = row.use_profit_target if row.use_profit_target is not None else True

    payload: dict[str, Any] = {
        "deployment_id": row.row_id,
        "enabled": row.enabled,
        "symbol": row.symbol,
        "strategy": {
            "key": "manual_breakout",
            "version": 1,
            "params": {
                "direction": row.direction,
                "trigger_price": row.trigger_price,
                "trigger_direction": row.trigger_direction,
                "vma_length": 10,
                "vma_timeframe": "5m",
            },
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": row.authorization_mode != "live",
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": row.end_in_days if row.end_in_days is not None else 5,
            "target_abs_delta_min": 0.30,
            "target_abs_delta_max": 0.70,
            "min_open_interest": 50,
            "max_bid_ask_spread_pct": 0.25,
        },
        "risk": {
            "profile": "manual_breakout_v1",
            "max_trade_premium_usd": row.max_trade_premium_usd if row.max_trade_premium_usd is not None else 300.0,
            "hard_flat_time_et": hard_flat_time_et,
            "stop_loss_pct": stop_loss_pct,
        },
        "exit": {
            "profile": "manual_breakout_exit_v1",
            "use_algorithmic_exit": True,
            "use_profit_target": use_profit_target,
            "profit_target_multiple": profit_target_multiple,
            "option_profit_target_pct": row.option_profit_target_pct,
            "stop_loss_pct": stop_loss_pct,
            "stop_to_breakeven_after_r_multiple": row.stop_to_breakeven_after_r_multiple,
            "hard_flat_time_et": hard_flat_time_et,
        },
        "source": {
            "origin": "active_sheet_manual",
            "metadata": {
                "manual_setup_type": _normalized_manual_setup_type(row.manual_setup_type),
            },
        },
    }
    if row.after_time_et is not None:
        payload["strategy"]["params"]["after_time_et"] = row.after_time_et
    if row.close_by_factor is not None:
        payload["strategy"]["params"]["close_by_factor"] = row.close_by_factor

    payload["execution"] = _apply_execution_overrides(payload["execution"], row)
    payload["risk"] = _apply_risk_overrides(payload["risk"], row)
    payload["exit"] = _apply_exit_overrides(payload["exit"], row)
    payload["source"] = _merge_source_metadata(payload["source"], row=row, origin="active_sheet_manual")
    return DeploymentManifest.model_validate(payload)


def _catalog_entry_payload(entry: StrategyCatalogEntry) -> dict[str, Any]:
    return {
        "deployment_id": entry.strategy_id,
        "enabled": entry.enabled,
        "symbol": entry.symbol,
        "strategy": entry.strategy.model_dump(mode="json"),
        "execution": entry.execution.model_dump(mode="json"),
        "risk": entry.risk.model_dump(mode="json"),
        "exit": entry.exit.model_dump(mode="json"),
        "source": entry.source.model_dump(mode="json"),
    }


def _google_catalog_entry_payload(
    entry: StrategyCatalogSheetRow,
    *,
    operator_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = operator_defaults or {}
    use_defaults = entry.mala_handoff_version is not None
    summary = entry.playbook_summary_json if isinstance(entry.playbook_summary_json, dict) else {}
    entry_params = dict(summary.get("entry_params") or {})
    vehicle_mapping = dict(summary.get("vehicle_mapping") or {})
    catastrophe_exit_params = dict(summary.get("catastrophe_exit_params") or {})
    thesis_exit_params = dict(summary.get("thesis_exit_params") or {})

    direction = str(entry.direction or entry_params.get("direction") or "").strip().lower() or None
    if direction and "direction" not in entry_params:
        entry_params["direction"] = direction
    for key in ("after_time_et",):
        if key in entry_params:
            entry_params[key] = normalize_time_text(str(entry_params[key]))

    default_stop_loss_pct = _coerce_float(defaults.get("option_stop_pct")) if use_defaults else None
    default_hard_flat_time = defaults.get("hard_flat_time_et") if use_defaults else None

    stop_loss_pct = _first_not_none(_coerce_float(catastrophe_exit_params.get("stop_loss_pct")), default_stop_loss_pct, 0.45)
    hard_flat_time_et = (
        normalize_time_text(str(catastrophe_exit_params.get("hard_flat_time_et") or default_hard_flat_time or "15:55"))
        or "15:55"
    )
    default_profit_target_enabled = (
        _coerce_bool(defaults.get("option_profit_target_enabled")) if use_defaults else None
    )
    default_option_profit_target_pct = (
        _coerce_float(defaults.get("option_profit_target_pct")) if use_defaults else None
    )
    default_target_approach_offset_pct = (
        _coerce_float(defaults.get("target_approach_offset_pct")) if use_defaults else None
    )
    default_target_pullback_restore_progress_pct = (
        _coerce_float(defaults.get("target_pullback_restore_progress_pct")) if use_defaults else None
    )
    use_profit_target = _coerce_bool(catastrophe_exit_params.get("use_profit_target"))
    if use_profit_target is None:
        use_profit_target = bool(default_profit_target_enabled and default_option_profit_target_pct is not None)
    profit_target_multiple = _coerce_float(catastrophe_exit_params.get("profit_target_multiple"))
    option_profit_target_pct = _coerce_float(catastrophe_exit_params.get("option_profit_target_pct"))
    if option_profit_target_pct is None and use_profit_target:
        option_profit_target_pct = default_option_profit_target_pct
    stop_to_breakeven_after_r_multiple = _coerce_float(
        catastrophe_exit_params.get("stop_to_breakeven_after_r_multiple")
    )
    strategy_key = str(entry.strategy_key or "").strip()
    use_algorithmic_exit = _google_catalog_use_algorithmic_exit(
        summary=summary,
        strategy_key=strategy_key,
        thesis_exit_policy=entry.thesis_exit_policy,
    )
    execution_payload: dict[str, Any] = {
        "profile": str(vehicle_mapping.get("profile") or "single_leg_long_premium_v1"),
        "shadow_only": True,
        "option_mapping": vehicle_mapping.get("option_mapping") or _option_mapping_from_structure(vehicle_mapping.get("structure")),
        "dte_fallback_policy": _first_not_none(
            _normalize_dte_fallback_policy(vehicle_mapping.get("dte_fallback_policy")),
            _normalize_dte_fallback_policy(defaults.get("dte_fallback_policy")) if use_defaults else None,
            "strict",
        ),
        "min_open_interest": _first_not_none(
            _coerce_int(vehicle_mapping.get("min_open_interest")),
            _coerce_int(defaults.get("min_open_interest")) if use_defaults else None,
            100,
        ),
        "max_bid_ask_spread_pct": _first_not_none(
            _coerce_float(vehicle_mapping.get("max_bid_ask_spread_pct")),
            _coerce_float(defaults.get("max_bid_ask_spread_pct")) if use_defaults else None,
            0.20,
        ),
    }
    dte_range = _compact_numeric_range_text(vehicle_mapping.get("dte") or vehicle_mapping.get("dte_target"))
    if dte_range is not None:
        execution_payload["dte"] = dte_range
    else:
        evidence_dte_min = _first_not_none(
            _coerce_int(entry.recommended_dte_min),
            _coerce_int(
                entry.thesis_exit_metrics_json.get("recommended_dte_min")
                if isinstance(entry.thesis_exit_metrics_json, dict)
                else None
            ),
        )
        evidence_dte_max = _first_not_none(
            _coerce_int(entry.recommended_dte_max),
            _coerce_int(
                entry.thesis_exit_metrics_json.get("recommended_dte_max")
                if isinstance(entry.thesis_exit_metrics_json, dict)
                else None
            ),
        )
        execution_payload["dte_min"] = (
            _first_not_none(
                _coerce_int(vehicle_mapping.get("dte_min")),
                evidence_dte_min,
                _coerce_int(defaults.get("dte_min")) if use_defaults else None,
                0,
            )
        )
        execution_payload["dte_max"] = (
            _first_not_none(
                _coerce_int(vehicle_mapping.get("dte_max")),
                evidence_dte_max,
                _coerce_int(defaults.get("dte_max")) if use_defaults else None,
                7,
            )
        )
    delta_range = _compact_numeric_range_text(vehicle_mapping.get("delta_target") or vehicle_mapping.get("delta_plan"))
    if delta_range is not None:
        execution_payload["delta_target"] = delta_range
    else:
        execution_payload["target_abs_delta_min"] = _first_not_none(
            _coerce_float(vehicle_mapping.get("target_abs_delta_min")),
            _coerce_float(defaults.get("delta_min")) if use_defaults else None,
        )
        execution_payload["target_abs_delta_max"] = _first_not_none(
            _coerce_float(vehicle_mapping.get("target_abs_delta_max")),
            _coerce_float(defaults.get("delta_max")) if use_defaults else None,
        )
    entry_window = vehicle_mapping.get("entry_window_et")
    if entry_window is not None:
        execution_payload["entry_window_et"] = entry_window
    elif use_defaults:
        signal_start, _signal_end = _split_signal_window(entry.signal_window_et)
        start = normalize_time_text(str(defaults.get("execution_window_start_et") or ""))
        end = normalize_time_text(str(defaults.get("execution_window_end_et") or ""))
        execution_start = signal_start or start
        if execution_start:
            execution_payload["entry_window_start_et"] = execution_start
        if end:
            execution_payload["entry_window_end_et"] = end

    default_max_premium = _coerce_float(defaults.get("max_trade_premium_usd")) if use_defaults else None
    risk_payload: dict[str, Any] = {
        "profile": f"{strategy_key}_risk_v1" if strategy_key else "catalog_promoted_v1",
        "max_trade_premium_usd": _first_not_none(default_max_premium, 300.0),
        "hard_flat_time_et": hard_flat_time_et,
        "stop_loss_pct": stop_loss_pct,
    }
    for default_key in (
        "max_open_positions_total",
        "max_open_positions_per_symbol",
        "max_open_positions_per_deployment",
    ):
        value = _coerce_int(defaults.get(default_key)) if use_defaults else None
        if value is not None:
            risk_payload[default_key] = value

    return {
        "strategy_id": entry.catalog_key,
        "enabled": True,
        "symbol": entry.symbol,
        "strategy": {
            "key": strategy_key,
            "version": 1,
            "params": entry_params,
        },
        "execution": execution_payload,
        "risk": risk_payload,
        "exit": {
            "profile": f"{strategy_key}_exit_v1" if strategy_key else "catalog_promoted_exit_v1",
            "use_algorithmic_exit": use_algorithmic_exit,
            "use_profit_target": use_profit_target,
            "profit_target_multiple": profit_target_multiple,
            "option_profit_target_pct": option_profit_target_pct,
            "target_approach_offset_pct": _first_not_none(
                _coerce_float(catastrophe_exit_params.get("target_approach_offset_pct")),
                default_target_approach_offset_pct,
            ),
            "target_pullback_restore_progress_pct": _first_not_none(
                _coerce_float(catastrophe_exit_params.get("target_pullback_restore_progress_pct")),
                default_target_pullback_restore_progress_pct,
            ),
            "stop_loss_pct": stop_loss_pct,
            "stop_to_breakeven_after_r_multiple": stop_to_breakeven_after_r_multiple,
            "hard_flat_time_et": hard_flat_time_et,
            "thesis_exit_policy": entry.thesis_exit_policy,
            "thesis_exit_params": thesis_exit_params,
            "catastrophe_exit_params": catastrophe_exit_params,
        },
        "source": {
            "origin": "google_strategy_catalog",
            "run_date": entry.last_validated_date or entry.first_validated_date,
            "artifact": entry.playbook_id,
            "metadata": _google_catalog_metadata(entry),
        },
        "approval_status": "approved",
        "tags": [
            tag
            for tag in (
                "google_promoted",
                entry.strategy_family,
                entry.direction,
                entry.bias_template,
            )
            if tag
        ],
    }


def _google_catalog_use_algorithmic_exit(
    *,
    summary: dict[str, Any],
    strategy_key: str,
    thesis_exit_policy: str | None,
) -> bool:
    exit_controls = summary.get("exit_controls")
    if isinstance(exit_controls, dict) and "use_algorithmic_exit" in exit_controls:
        explicit = _coerce_bool(exit_controls.get("use_algorithmic_exit"))
        if explicit is not None:
            return explicit

    if thesis_exit_policy:
        return False

    return strategy_key in NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _atomic_yaml_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _normalize_mala_evidence_row(normalized: dict[str, Any]) -> dict[str, Any]:
    row = dict(normalized)
    strategy_params = row.get("strategy_params_json")
    if not isinstance(strategy_params, dict):
        strategy_params = {}
    thesis_exit_params = row.get("thesis_exit_params_json")
    if not isinstance(thesis_exit_params, dict):
        thesis_exit_params = {}
    thesis_exit_metrics = row.get("thesis_exit_metrics_json")
    if not isinstance(thesis_exit_metrics, dict):
        thesis_exit_metrics = {}

    recommendation_tier = str(row.get("recommendation_tier") or "").strip().lower()
    thesis_exit_tested = _coerce_bool(row.get("thesis_exit_tested"))
    strategy_key = str(row.get("strategy_key") or "").strip().lower()
    thesis_exit_policy = str(row.get("thesis_exit_policy") or "").strip() or None
    strategy_variant = str(row.get("strategy_variant") or "").strip() or derive_strategy_variant(
        strategy_key=strategy_key,
        strategy_name=str(row.get("strategy_name") or ""),
        strategy_params=strategy_params,
    )
    capability = evaluate_strategy_capability(
        strategy_key=strategy_key,
        strategy_variant=strategy_variant,
        strategy_name=str(row.get("strategy_name") or ""),
        strategy_params=strategy_params,
        thesis_exit_policy=thesis_exit_policy,
    )
    row["playbook_id"] = row.get("playbook_id") or row.get("hypothesis_id")
    row["strategy_family"] = row.get("strategy_family") or row.get("strategy_key")
    row["lifecycle_status"] = row.get("lifecycle_status") or ("candidate" if recommendation_tier != "watch_only" else "hold")
    row["strategy_variant"] = capability.strategy_variant
    row["bhiksha_capability_status"] = capability.status
    row["bhiksha_capability_reason"] = capability.reason
    explicit_bhiksha_ready = _coerce_bool(row.get("bhiksha_ready"))
    explicit_runtime_supported = _coerce_bool(row.get("bhiksha_runtime_supported"))
    if explicit_runtime_supported is not None:
        runtime_ready_asserted = explicit_runtime_supported
    elif explicit_bhiksha_ready is not None:
        runtime_ready_asserted = explicit_bhiksha_ready
    else:
        runtime_ready_asserted = True
    row["bhiksha_ready"] = bool(runtime_ready_asserted and capability.supported)
    row["validation_count"] = row.get("validation_count") or row.get("signal_count")
    row["last_validated_date"] = row.get("last_validated_date") or _run_date_from_path(row.get("run_dir"))
    if row.get("playbook_summary_json") is None:
        direction = str(row.get("direction") or "").strip().lower()
        entry_params = dict(strategy_params)
        if direction and "direction" not in entry_params:
            entry_params["direction"] = direction
        row["playbook_summary_json"] = {
            "entry_params": entry_params,
            "thesis_exit_params": thesis_exit_params,
            "mala_evidence": {
                "mala_handoff_version": row.get("mala_handoff_version"),
                "strategy_name": row.get("strategy_name"),
                "strategy_variant": capability.strategy_variant,
                "bhiksha_capability_status": capability.status,
                "bhiksha_capability_reason": capability.reason,
                "signal_window_et": row.get("signal_window_et"),
                "signal_window_derivation": row.get("signal_window_derivation"),
                "recommendation_tier": row.get("recommendation_tier"),
                "recommendation_tier_reason": row.get("recommendation_tier_reason"),
                "recommendation_checks": row.get("recommendation_checks_json"),
                "thesis_exit_tested": thesis_exit_tested,
                "thesis_exit_metrics": thesis_exit_metrics,
                "exit_reliability": row.get("exit_reliability"),
                "exit_trade_count": row.get("exit_trade_count"),
                "warnings": row.get("warnings"),
            },
        }
    return row


def _run_date_from_path(value: Any) -> str | None:
    text = str(value or "")
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def _option_mapping_from_structure(value: Any) -> dict[str, str]:
    structure = str(value or "").strip().lower()
    if structure in {"long_put", "put"}:
        return {"long_signal": "CALL", "short_signal": "PUT"}
    if structure in {"long_call", "call"}:
        return {"long_signal": "CALL", "short_signal": "PUT"}
    return {"long_signal": "CALL", "short_signal": "PUT"}


_COMPACT_NUMERIC_RANGE_RE = re.compile(r"(?P<start>\d+(?:\.\d+)?)(?:\s*-\s*(?P<end>\d+(?:\.\d+)?))?")


def _compact_numeric_range_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    match = _COMPACT_NUMERIC_RANGE_RE.search(str(value))
    if match is None:
        return None
    start = match.group("start")
    end = match.group("end")
    return f"{start}-{end}" if end is not None else start


def _split_signal_window(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    if "-" not in text:
        return normalize_time_text(text), None
    start, end = text.split("-", 1)
    return normalize_time_text(start), normalize_time_text(end)


def _apply_execution_overrides(section: dict[str, Any], row: ActivePlanSheetRow) -> dict[str, Any]:
    updated = _deep_merge(section, row.execution_overrides)
    updated["shadow_only"] = row.authorization_mode != "live"
    if row.entry_window_start_et is not None:
        updated["entry_window_start_et"] = row.entry_window_start_et
    if row.entry_window_end_et is not None:
        updated["entry_window_end_et"] = row.entry_window_end_et
    return updated


def _apply_risk_overrides(section: dict[str, Any], row: ActivePlanSheetRow) -> dict[str, Any]:
    updated = _deep_merge(section, row.risk_overrides)
    if row.max_trade_premium_usd is not None:
        updated["max_trade_premium_usd"] = row.max_trade_premium_usd
    if row.stop_loss_pct is not None:
        updated["stop_loss_pct"] = row.stop_loss_pct
    if row.hard_flat_time_et is not None:
        updated["hard_flat_time_et"] = row.hard_flat_time_et
    return updated


def _apply_exit_overrides(section: dict[str, Any], row: ActivePlanSheetRow) -> dict[str, Any]:
    updated = _deep_merge(section, row.exit_overrides)
    # Track every ExitSpec key the operator set explicitly — via the
    # ``exit_overrides`` deep-merge dict OR a dedicated typed column — so the
    # published profile spec below cannot silently clobber them with its kernel
    # defaults (e.g. stop_loss_pct=0.45, hard_flat_time_et="15:55").
    explicit_keys: set[str] = set(row.exit_overrides or {})
    if row.use_profit_target is not None:
        updated["use_profit_target"] = row.use_profit_target
        explicit_keys.add("use_profit_target")
    if row.profit_target_multiple is not None:
        updated["profit_target_multiple"] = row.profit_target_multiple
        explicit_keys.add("profit_target_multiple")
    if row.option_profit_target_pct is not None:
        updated["option_profit_target_pct"] = row.option_profit_target_pct
        explicit_keys.add("option_profit_target_pct")
    if row.stop_loss_pct is not None:
        updated["stop_loss_pct"] = row.stop_loss_pct
        explicit_keys.add("stop_loss_pct")
    if row.hard_flat_time_et is not None:
        updated["hard_flat_time_et"] = row.hard_flat_time_et
        explicit_keys.add("hard_flat_time_et")
    if row.stop_to_breakeven_after_r_multiple is not None:
        updated["stop_to_breakeven_after_r_multiple"] = row.stop_to_breakeven_after_r_multiple
        explicit_keys.add("stop_to_breakeven_after_r_multiple")
    updated = _apply_exit_profile_spec(updated, row, explicit_keys=explicit_keys)
    return updated


# Field map: kernel ``ManagementPolicySpec`` attribute -> Bhiksha ``ExitSpec``
# field. These are the v2 operator exit-profile dials that the live
# ``bhiksha.execution.profile_exit`` evaluator reads off ``deployment.exit``.
# Mirrors ``ProfileExitFields.from_management_spec`` (the runtime adapter) so the
# config bridge and the evaluator agree on the contract. ``policy_id`` lands on
# ``profile_exit_id`` (which is what arms the profile-exit path), and
# ``option_stop_fallback_pct`` lands on ``exit.stop_loss_pct`` (the resolvable
# recovery-stop the DeploymentManifest profile-stop validator requires).
_EXIT_PROFILE_SPEC_FIELD_MAP: dict[str, str] = {
    "policy_id": "profile_exit_id",
    "target_1_r": "target_1_r",
    "target_2_r": "target_2_r",
    "target_1_quantity": "target_1_quantity",
    "initial_stop_pct": "initial_stop_pct",
    "premium_disaster_stop_pct": "premium_disaster_stop_pct",
    "no_progress_seconds": "no_progress_seconds",
    "max_hold_seconds": "max_hold_seconds",
    "high_water_giveback_policy": "high_water_giveback_policy",
    "breakeven_after_t1": "breakeven_after_t1",
    "eod_flat": "eod_flat",
    "no_progress_favorable_floor_r": "no_progress_favorable_floor_r",
    "option_stop_fallback_pct": "stop_loss_pct",
    "hard_flat_time_et": "hard_flat_time_et",
}


def _exit_spec_fields_from_management_policy_spec(spec_payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a kernel ``ManagementPolicySpec`` cell and map it onto ExitSpec.

    The raw JSON cell is round-tripped through the canonical kernel
    ``ManagementPolicySpec`` model so the giveback enum and the numeric bounds
    (e.g. ``target_1_quantity`` in [0, 1], stop pcts in [0, 1]) are enforced by
    the kernel's own validators before anything reaches the Bhiksha config. The
    returned dict is shaped for ``ExitSpec`` and merged into the compiled exit
    payload; ``ExitSpec``'s own validators (disaster>=initial stop ordering) and
    the ``DeploymentManifest`` profile-recovery-stop check then run on top.

    NB: ``profile_exit_drives_live`` is intentionally NOT mapped here. The bridge
    only carries the profile into the config in SHADOW (record-only) form; live
    enablement stays the operator's separate, explicit switch.
    """
    from bhiksha.shared_kernel import ensure_kernel_on_path

    ensure_kernel_on_path()
    from mala_bhiksha_kernel import ManagementPolicySpec  # noqa: PLC0415

    spec = ManagementPolicySpec.model_validate(spec_payload)
    mapped: dict[str, Any] = {}
    for spec_field, exit_field in _EXIT_PROFILE_SPEC_FIELD_MAP.items():
        value = getattr(spec, spec_field, None)
        if value is None:
            continue
        mapped[exit_field] = value
    return mapped


def _apply_exit_profile_spec(
    section: dict[str, Any],
    row: ActivePlanSheetRow,
    explicit_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Carry an assigned exit-profile (kernel ``ManagementPolicySpec`` cell) onto ExitSpec.

    Back-compat: a row WITHOUT ``exit_profile_spec`` is returned untouched, so
    its compiled ExitSpec is byte-identical to pre-bridge output. A row WITH a
    spec gets the mapped v2 fields layered onto the exit payload. An explicit
    per-row knob still wins (it is the operator's deliberate last-mile value), so
    any ExitSpec key the operator set — either through an ``exit_overrides`` entry
    or a dedicated typed column (passed in via ``explicit_keys``, e.g.
    ``stop_loss_pct`` / ``hard_flat_time_et``) — is protected from the published
    spec, whose kernel DEFAULTS would otherwise silently clobber it.
    """
    spec_payload = row.exit_profile_spec
    if not spec_payload:
        return section
    if not isinstance(spec_payload, dict):
        raise ValueError(
            "exit_profile_spec must be a JSON object carrying a kernel "
            f"ManagementPolicySpec, got {type(spec_payload).__name__}"
        )
    mapped = _exit_spec_fields_from_management_policy_spec(spec_payload)
    # The operator's explicit per-row knobs (exit_overrides dict + dedicated
    # typed columns) take precedence over the published profile spec.
    protected_keys = set(row.exit_overrides or {})
    if explicit_keys:
        protected_keys |= explicit_keys
    existing_stop_loss_pct = _coerce_float(section.get("stop_loss_pct"))
    if (
        "stop_loss_pct" not in protected_keys
        and existing_stop_loss_pct is not None
        and existing_stop_loss_pct > 0
        and abs(existing_stop_loss_pct - 0.45) > 1e-9
    ):
        # Treat option_stop_fallback_pct as a true fallback. If the active section
        # already resolved a non-default stop from catalog/defaults (for example
        # Operator_Defaults_v1 option_stop_pct=0.35), the profile spec must not
        # silently widen that max-risk control.
        protected_keys.add("stop_loss_pct")
    updated = dict(section)
    for key, value in mapped.items():
        if key in protected_keys:
            continue
        updated[key] = value
    return updated


def _merge_source_metadata(
    source: dict[str, Any],
    *,
    row: ActivePlanSheetRow,
    origin: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = dict(source)
    metadata = dict(updated.get("metadata") or {})
    metadata.update(
        {
            "sheet_row_id": row.row_id,
            "row_type": row.row_type,
            "authorization_mode": row.authorization_mode,
        }
    )
    if row.notes:
        metadata["notes"] = row.notes
    metadata.update(row.source_metadata)
    if extra_metadata:
        metadata.update(extra_metadata)
    updated["origin"] = origin
    updated["metadata"] = metadata
    return updated


def _manual_strategy_catalog_ids(catalog_root: Path, generated_root: Path) -> set[str]:
    if not catalog_root.exists():
        return set()
    strategy_ids: set[str] = set()
    for file_path in sorted(catalog_root.rglob("*.yaml")):
        if file_path.is_relative_to(generated_root):
            continue
        payload = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, dict) and payload.get("strategy_id"):
            strategy_ids.add(str(payload["strategy_id"]))
    return strategy_ids


def _is_google_catalog_entry_promotable(entry: StrategyCatalogSheetRow, supported_keys: set[str]) -> bool:
    strategy_key = str(entry.strategy_key or "").strip()
    try:
        _validate_google_catalog_exit_contract(entry.catalog_key, entry)
    except ValueError:
        return False
    capability_supported = True
    if _uses_bhiksha_capability_contract(entry):
        capability = evaluate_strategy_capability(
            strategy_key=strategy_key,
            strategy_variant=entry.strategy_variant,
            strategy_name=entry.strategy_name,
            strategy_params=entry.strategy_params_json if isinstance(entry.strategy_params_json, dict) else None,
            thesis_exit_policy=entry.thesis_exit_policy,
        )
        capability_supported = capability.supported
    return (
        entry.bhiksha_ready
        and entry.lifecycle_status in {"active", "candidate"}
        and bool(entry.catalog_key)
        and bool(entry.symbol)
        and strategy_key in supported_keys
        and capability_supported
    )


def _normalize_sheet_mapping(data: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in data.items():
        key = _normalize_key(str(raw_key))
        key = _COLUMN_ALIASES.get(key, key)
        value = _normalize_value(raw_value)
        if key in normalized and normalized[key] is not None and value is None:
            continue
        normalized[key] = value
    for key in ("after_time_et", "entry_window_start_et", "entry_window_end_et", "hard_flat_time_et"):
        normalized[key] = normalize_time_text(normalized.get(key))
    return normalized


def _prepare_sheet_row_payload(
    data: dict[str, Any],
    *,
    row_type: Literal["strategy", "manual"] | None = None,
    row_index: int | None = None,
    sheet_name: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_sheet_mapping(dict(data))
    if row_type is not None and normalized.get("row_type") is None:
        normalized["row_type"] = row_type
    if row_index is not None and normalized.get("row_index") is None:
        normalized["row_index"] = row_index
    normalized.setdefault("source_metadata", {})
    if sheet_name is not None:
        normalized["source_metadata"] = {
            **(normalized.get("source_metadata") or {}),
            "sheet_name": sheet_name,
        }
    if normalized.get("row_index") is not None:
        normalized["source_metadata"] = {
            **(normalized.get("source_metadata") or {}),
            "row_index": normalized["row_index"],
        }
    if normalized.get("row_id") is None:
        normalized["row_id"] = _default_row_id(normalized)
    return normalized


def _default_row_id(normalized: dict[str, Any]) -> str:
    row_type = str(normalized.get("row_type") or "row").strip().lower()
    row_index = normalized.get("row_index")
    if row_type == "strategy":
        parts = [
            "strategy",
            normalized.get("strategy_id"),
            normalized.get("authorization_mode") or "shadow",
            f"row_{row_index}" if row_index is not None else None,
        ]
    elif row_type == "manual":
        parts = [
            _normalized_manual_setup_type(normalized.get("manual_setup_type") or normalized.get("strategy_id")),
            normalized.get("symbol"),
            normalized.get("direction"),
            normalized.get("trigger_direction"),
            normalized.get("trigger_price"),
            f"row_{row_index}" if row_index is not None else None,
        ]
    else:
        parts = [row_type, f"row_{row_index}" if row_index is not None else None]
    return _slugify(*parts)


def _slugify(*parts: Any) -> str:
    tokens: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip().lower()
        if not text:
            continue
        slug = []
        for char in text:
            slug.append(char if char.isalnum() else "_")
        collapsed = "".join(slug).strip("_")
        while "__" in collapsed:
            collapsed = collapsed.replace("__", "_")
        if collapsed:
            tokens.append(collapsed)
    return "_".join(tokens) or "sheet_row"


def _should_skip_prepared_row(payload: dict[str, Any]) -> bool:
    if payload.get("enabled") is False:
        return True
    row_type = payload.get("row_type")
    if row_type == "strategy":
        return payload.get("strategy_id") is None
    if row_type == "manual":
        return not any(
            payload.get(key) is not None
            for key in ("manual_setup_type", "strategy_id", "symbol", "direction", "trigger_price", "trigger_direction")
        )
    return False


def _google_catalog_metadata(entry: StrategyCatalogSheetRow | None) -> dict[str, Any]:
    if entry is None:
        return {}
    metadata = {
        "catalog_key": entry.catalog_key,
        "playbook_id": entry.playbook_id,
        "bias_template": entry.bias_template,
        "strategy_key": entry.strategy_key,
        "strategy_family": entry.strategy_family,
        "direction": entry.direction,
        "lifecycle_status": entry.lifecycle_status,
        "operator_status_override": entry.operator_status_override,
        "operator_notes": entry.operator_notes,
        "bhiksha_ready": entry.bhiksha_ready,
        "first_validated_date": entry.first_validated_date,
        "last_validated_date": entry.last_validated_date,
        "validation_count": entry.validation_count,
        "expectancy": entry.expectancy,
        "confidence": entry.confidence,
        "signal_count": entry.signal_count,
        "execution_robustness": entry.execution_robustness,
        "thesis_exit_policy": entry.thesis_exit_policy,
        "mala_handoff_version": entry.mala_handoff_version,
        "hypothesis_id": entry.hypothesis_id,
        "strategy_name": entry.strategy_name,
        "strategy_variant": entry.strategy_variant,
        "bhiksha_capability_status": entry.bhiksha_capability_status,
        "bhiksha_capability_reason": entry.bhiksha_capability_reason,
        "provider_validation_status": entry.provider_validation_status,
        "provider_feature_risk": entry.provider_feature_risk,
        "provider_signal_overlap": entry.provider_signal_overlap,
        "provider_validation_report": entry.provider_validation_report,
        "bhiksha_runtime_supported": entry.bhiksha_runtime_supported,
        "bhiksha_runtime_reason": entry.bhiksha_runtime_reason,
        "mala_evidence_ready": entry.mala_evidence_ready,
        "mala_evidence_blocking_checks": entry.mala_evidence_blocking_checks,
        "activation_candidate": entry.activation_candidate,
        "activation_blocking_checks": entry.activation_blocking_checks,
        "m7_status": entry.m7_status,
        "m7_feature_risk": entry.m7_feature_risk,
        "m7_signal_overlap": entry.m7_signal_overlap,
        "triage_verdict": entry.triage_verdict,
        "triage_verdict_reason": entry.triage_verdict_reason,
        "triage_blocking_checks": entry.triage_blocking_checks,
        "triage_advisory_notes": entry.triage_advisory_notes,
        "triage_artifact": entry.triage_artifact,
        "signal_window_et": entry.signal_window_et,
        "signal_window_derivation": entry.signal_window_derivation,
        "recommendation_tier": entry.recommendation_tier,
        "recommendation_tier_reason": entry.recommendation_tier_reason,
        "recommendation_checks": entry.recommendation_checks_json,
        "m5_execution_profile": entry.m5_execution_profile,
        "m5_stress_profile": entry.m5_stress_profile,
        "thesis_exit_tested": entry.thesis_exit_tested,
        "option_trade_ready": entry.option_trade_ready,
        "option_adjusted_expectancy_pct": entry.option_adjusted_expectancy_pct,
        "option_exit_quality": entry.option_exit_quality,
        "recommended_dte_min": entry.recommended_dte_min,
        "recommended_dte_max": entry.recommended_dte_max,
        "theta_penalty_pct": entry.theta_penalty_pct,
        "expectancy_pct": entry.expectancy_pct,
        "avg_win_pct": entry.avg_win_pct,
        "avg_loss_pct_abs": entry.avg_loss_pct_abs,
        "pnl_pct_per_minute": entry.pnl_pct_per_minute,
        "pnl_pct_per_bar": entry.pnl_pct_per_bar,
        "median_minutes_held": entry.median_minutes_held,
        "avg_minutes_held": entry.avg_minutes_held,
        "target_hit_rate": entry.target_hit_rate,
        "stop_loss_rate": entry.stop_loss_rate,
        "target_hit_within_15_minutes": entry.target_hit_within_15_minutes,
        "target_hit_within_30_minutes": entry.target_hit_within_30_minutes,
        "stop_loss_within_15_minutes": entry.stop_loss_within_15_minutes,
        "exit_reliability": entry.exit_reliability,
        "exit_trade_count": entry.exit_trade_count,
        "exit_contract_status": "ok" if _uses_bhiksha_capability_contract(entry) else None,
        "exit_contract_reason": "mala_thesis_exit_loaded" if _uses_bhiksha_capability_contract(entry) else None,
        "warnings": entry.warnings,
        "exit_profile_spec": entry.exit_profile_spec,
        "playbook_summary": _normalized_playbook_summary_metadata(entry),
        "catalog_row_index": entry.row_index,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _normalized_playbook_summary_metadata(entry: StrategyCatalogSheetRow) -> dict[str, Any] | list[Any] | str | None:
    summary = entry.playbook_summary_json
    if not isinstance(summary, dict):
        return summary
    normalized = copy.deepcopy(summary)
    compatibility = normalized.get("bhiksha_compatibility")
    if isinstance(compatibility, dict):
        canonical: dict[str, Any] = {
            "bhiksha_ready": entry.bhiksha_ready,
            "supported": entry.bhiksha_ready,
        }
        if "has_optimized_thesis_exit" in compatibility:
            canonical["has_optimized_thesis_exit"] = compatibility["has_optimized_thesis_exit"]
        if entry.bhiksha_ready:
            canonical["note"] = "bhiksha strategy and exit policy both implemented"
        elif "note" in compatibility:
            canonical["note"] = compatibility["note"]
        normalized["bhiksha_compatibility"] = canonical
    return normalized


def _validate_google_catalog_alignment(
    strategy_id: str,
    local_entry: StrategyCatalogEntry,
    google_entry: StrategyCatalogSheetRow,
    *,
    live: bool = True,
) -> list[str]:
    """Validate a Sheet catalog row against the local entry before compiling.

    Two gate classes:
      * SAFETY/INTEGRITY gates always raise regardless of ``live`` — runtime
        capability, bhiksha_ready, explicit m7 ``block``, triage ``KILL``,
        retired lifecycle, symbol/strategy_key mismatch. A row that fails
        these cannot produce meaningful trades in any mode.
      * EVIDENCE-QUALITY gates (mala_evidence_ready / activation_candidate /
        option_trade_ready) raise only for ``live`` rows. Shadow rows are the
        instrument that GATHERS this evidence (paper fills on the real feed),
        so blocking shadow on missing activation evidence is circular.

    Returns the list of relaxed gate descriptions (empty for live rows or
    fully-passing rows) so the caller can stamp them into deployment
    metadata — a shadow lane running on sub-activation evidence must be
    visibly labeled, and any future live promotion re-runs the full gate.
    """
    if _uses_bhiksha_capability_contract(google_entry):
        capability = evaluate_strategy_capability(
            strategy_key=google_entry.strategy_key,
            strategy_variant=google_entry.strategy_variant,
            strategy_name=google_entry.strategy_name,
            strategy_params=google_entry.strategy_params_json if isinstance(google_entry.strategy_params_json, dict) else None,
            thesis_exit_policy=google_entry.thesis_exit_policy,
        )
        if not capability.supported:
            raise ValueError(
                f"unsupported_strategy_variant: {capability.strategy_key}.{capability.strategy_variant} {capability.reason}"
            )
    if not google_entry.bhiksha_ready:
        raise ValueError(f"Strategy {strategy_id!r} is not Bhiksha runtime-ready in Mala_Evidence_v1")
    relaxed_gates: list[str] = []
    if google_entry.mala_evidence_ready is False:
        reason = google_entry.mala_evidence_blocking_checks or "mala_evidence_ready=false"
        if live:
            raise ValueError(f"Strategy {strategy_id!r} is not mala_evidence_ready in Mala_Evidence_v1: {reason}")
        relaxed_gates.append(f"mala_evidence_ready: {reason}")
    if google_entry.activation_candidate is False:
        reason = google_entry.activation_blocking_checks or google_entry.triage_blocking_checks or "activation_candidate=false"
        if live:
            raise ValueError(f"Strategy {strategy_id!r} is not activation_candidate in Mala_Evidence_v1: {reason}")
        relaxed_gates.append(f"activation_candidate: {reason}")
    if google_entry.option_trade_ready is False:
        if live:
            raise ValueError(f"Strategy {strategy_id!r} is not option_trade_ready in Mala_Evidence_v1")
        relaxed_gates.append("option_trade_ready: option_trade_ready=false")
    if str(google_entry.m7_status or "").strip().lower() == "block":
        raise ValueError(f"Strategy {strategy_id!r} has m7_status=block in Mala_Evidence_v1")
    if str(google_entry.triage_verdict or "").strip().lower() == "kill":
        reason = google_entry.triage_verdict_reason or google_entry.triage_blocking_checks or "triage_verdict=KILL"
        raise ValueError(f"Strategy {strategy_id!r} has triage_verdict=KILL in Mala_Evidence_v1: {reason}")
    if google_entry.lifecycle_status == "retired":
        raise ValueError(f"Strategy {strategy_id!r} is retired in Google strategy catalog")
    if google_entry.symbol and google_entry.symbol != local_entry.symbol:
        raise ValueError(
            f"Google strategy catalog symbol mismatch for {strategy_id!r}: "
            f"{google_entry.symbol!r} vs local {local_entry.symbol!r}"
        )
    if google_entry.strategy_key and google_entry.strategy_key != local_entry.strategy.key:
        raise ValueError(
            f"Google strategy catalog strategy_key mismatch for {strategy_id!r}: "
            f"{google_entry.strategy_key!r} vs local {local_entry.strategy.key!r}"
        )
    return relaxed_gates


def _validate_google_catalog_exit_contract(strategy_id: str, google_entry: StrategyCatalogSheetRow) -> None:
    if not _uses_bhiksha_capability_contract(google_entry):
        return
    if google_entry.mala_handoff_version is None:
        return
    if google_entry.thesis_exit_tested is not True:
        raise ValueError(f"exit_contract_missing: Strategy {strategy_id!r} has thesis_exit_tested={google_entry.thesis_exit_tested!r}")
    if not google_entry.thesis_exit_policy:
        raise ValueError(f"exit_contract_missing: Strategy {strategy_id!r} is missing thesis_exit_policy")
    if not isinstance(google_entry.thesis_exit_metrics_json, dict) or not google_entry.thesis_exit_metrics_json:
        raise ValueError(f"exit_contract_missing: Strategy {strategy_id!r} is missing thesis_exit_metrics_json")
    if not isinstance(google_entry.thesis_exit_params_json, dict):
        raise ValueError(f"exit_contract_invalid: Strategy {strategy_id!r} has invalid thesis_exit_params_json")
    if _missing_required_thesis_exit_params(google_entry.thesis_exit_policy, google_entry.thesis_exit_params_json):
        missing = ", ".join(_missing_required_thesis_exit_params(google_entry.thesis_exit_policy, google_entry.thesis_exit_params_json))
        raise ValueError(
            f"exit_contract_missing: Strategy {strategy_id!r} thesis_exit_policy={google_entry.thesis_exit_policy!r} "
            f"is missing params: {missing}"
        )


def _missing_required_thesis_exit_params(policy: str, params: dict[str, Any]) -> list[str]:
    required_by_policy = {
        "fixed_rr_underlying": ["stop_loss_underlying_pct", "take_profit_underlying_r_multiple"],
        "time_stop_underlying": ["exit_time_et"],
        "trailing_vma_underlying": ["vma_col"],
        "ma_trailing_underlying": ["ma_col"],
        "ma_crossover_underlying": ["fast_ma_col", "slow_ma_col"],
        "atr_trailing_underlying": ["atr_col", "atr_multiple"],
    }
    return [key for key in required_by_policy.get(policy, []) if params.get(key) in (None, "")]


def _uses_bhiksha_capability_contract(entry: StrategyCatalogSheetRow) -> bool:
    return bool(
        entry.mala_handoff_version is not None
        or entry.strategy_variant
        or entry.bhiksha_capability_status
        or entry.bhiksha_capability_reason
    )


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_trigger_direction(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {">", ">=", "ABOVE", "AT_OR_ABOVE", "GTE"}:
        return "ABOVE"
    if normalized in {"<", "<=", "BELOW", "AT_OR_BELOW", "LTE"}:
        return "BELOW"
    return normalized


def _normalized_manual_setup_type(value: str | None) -> str:
    manual_setup = (value or "manual_trigger").strip().lower()
    if manual_setup in {"trigger", "manual_trigger"}:
        return "manual_trigger"
    if manual_setup in {"breakout", "manual_breakout"}:
        return "breakout"
    return manual_setup


def _normalize_dte_fallback_policy(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        lowered = stripped.lower()
        if lowered in {"true", "yes", "y"}:
            return True
        if lowered in {"false", "no", "n"}:
            return False
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        return stripped
    return value


def _coerce_bool(value: Any) -> bool | None:
    normalized = _normalize_value(value)
    if isinstance(normalized, bool):
        return normalized
    if isinstance(normalized, (int, float)):
        return bool(normalized)
    if isinstance(normalized, str):
        lowered = normalized.strip().lower()
        if lowered in {"1", "on"}:
            return True
        if lowered in {"0", "off"}:
            return False
    return None


def _looks_like_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if len(stripped) != 10:
        return False
    return stripped[4] == "-" and stripped[7] == "-" and stripped.replace("-", "").isdigit()


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _suppressed_row(
    *,
    reason: str,
    row: ActivePlanSheetRow | None = None,
    payload: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    row_type: str | None = None,
) -> dict[str, Any]:
    metadata = dict(
        (row.source_metadata if row is not None else payload.get("source_metadata")) or {}
    ) if (row is not None or payload is not None) else {}
    if row is not None:
        effective_row_type = row.row_type
        row_id = row.row_id
    else:
        effective_row_type = row_type or (payload.get("row_type") if payload is not None else None) or "unknown"
        row_id = payload.get("row_id") if payload is not None else None
    entry = {
        "action": "suppressed",
        "reason": reason,
        "row_id": row_id,
        "row_index": metadata.get("row_index"),
        "row_type": effective_row_type,
        "sheet_name": metadata.get("sheet_name"),
    }
    if raw is not None:
        entry["raw"] = {str(key): value for key, value in raw.items()}
    return entry


_COLUMN_ALIASES = {
    "id": "row_id",
    "deployment_id": "row_id",
    "type": "row_type",
    "mode": "authorization_mode",
    "strategy": "strategy_id",
    "setup": "manual_setup_type",
    "entry_trigger": "trigger_price",
    "trigger": "trigger_price",
    "trigger_level": "trigger_price",
    "entry_condition": "trigger_direction",
    "trigger_when": "trigger_direction",
    "after": "after_time_et",
    "days": "end_in_days",
    "dte": "end_in_days",
    "end_in_days": "end_in_days",
    "not_before": "after_time_et",
    "entry_after_et": "after_time_et",
    "max_premium": "max_trade_premium_usd",
    "premium_cap": "max_trade_premium_usd",
    "stop_pct": "stop_loss_pct",
    "target_r": "profit_target_multiple",
    "profit_target_r": "profit_target_multiple",
    "target_pct": "option_profit_target_pct",
    "profit_target_pct": "option_profit_target_pct",
    "option_target_pct": "option_profit_target_pct",
    "option_profit_target": "option_profit_target_pct",
    "flat_time": "hard_flat_time_et",
    "hard_flat": "hard_flat_time_et",
    "start": "entry_window_start_et",
    "end": "entry_window_end_et",
    "strategy_params": "strategy_params_override",
    "strategy_overrides": "strategy_params_override",
    "execution": "execution_overrides",
    "risk": "risk_overrides",
    "exit": "exit_overrides",
    "management_policy_spec": "exit_profile_spec",
    "exit_profile": "exit_profile_spec",
    "exit_profile_spec": "exit_profile_spec",
    "metadata": "source_metadata",
}
