"""Compile a sheet-style operator plan into Bhiksha's active plan contract."""

from __future__ import annotations

import csv
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import yaml

from bhiksha.config.loader import load_strategy_catalog
from bhiksha.config.models import ActivePlan, DeploymentManifest, StrategyCatalogEntry
from bhiksha.integrations.google_sheets import GoogleSheetTableClient
from bhiksha.strategy.capabilities import NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS
from bhiksha.strategy.registry import default_strategy_registry
from bhiksha.time_utils import normalize_time_text


class StrategyCatalogSheetRow(BaseModel):
    """Read-only metadata row loaded from the Google strategy catalog tab."""

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
    row_index: int | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = _normalize_sheet_mapping(data)
        if normalized.get("symbol") is not None:
            normalized["symbol"] = str(normalized["symbol"]).strip().upper()
        for key in ("strategy_key", "strategy_family", "direction", "lifecycle_status", "operator_status_override"):
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
    stop_to_breakeven_after_r_multiple: float | None = None
    entry_window_start_et: str | None = None
    entry_window_end_et: str | None = None
    notes: str | None = None
    strategy_params_override: dict[str, Any] = Field(default_factory=dict)
    execution_overrides: dict[str, Any] = Field(default_factory=dict)
    risk_overrides: dict[str, Any] = Field(default_factory=dict)
    exit_overrides: dict[str, Any] = Field(default_factory=dict)
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
    suppressed: list[dict[str, Any]] | None = None,
) -> CompiledActivePlan:
    suppressed_rows = list(suppressed or [])
    if google_strategy_catalog is not None:
        sync_google_strategy_catalog(
            strategy_catalog_path=strategy_catalog_path,
            google_strategy_catalog=google_strategy_catalog,
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
    strategy_client: GoogleSheetTableClient | None = None,
    manual_client: GoogleSheetTableClient | None = None,
    catalog_client: GoogleSheetTableClient | None = None,
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

    catalog_validation = load_strategy_catalog_sheet_rows_with_report(
        catalog_client.read_rows(),
        sheet_name=catalog_client.sheet_name,
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
            "strategy_sheet_name": strategy_client.sheet_name,
            "manual_sheet_name": manual_client.sheet_name,
        },
        google_strategy_catalog=catalog_validation.rows,
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

    stale_files = sorted(generated_root.rglob("*.yaml"))
    for stale_file in stale_files:
        stale_file.unlink()

    written_paths: list[Path] = []
    for entry in eligible_entries:
        output_path = generated_root / f"{entry.catalog_key}.yaml"
        output_path.write_text(
            yaml.safe_dump(_google_catalog_entry_payload(entry), sort_keys=False),
            encoding="utf-8",
        )
        written_paths.append(output_path)
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
    google_catalog_entry = google_catalog_by_id.get(strategy_id)
    if enforce_google_catalog and google_catalog_entry is None:
        raise ValueError(f"Strategy {strategy_id!r} is not present in Google strategy catalog")
    if google_catalog_entry is not None:
        _validate_google_catalog_alignment(strategy_id, entry, google_catalog_entry)

    payload = _catalog_entry_payload(entry)
    payload["deployment_id"] = row.row_id
    payload["enabled"] = row.enabled
    payload["execution"] = _apply_execution_overrides(payload["execution"], row)
    payload["risk"] = _apply_risk_overrides(payload["risk"], row)
    payload["exit"] = _apply_exit_overrides(payload["exit"], row)
    payload["strategy"]["params"] = _deep_merge(payload["strategy"]["params"], row.strategy_params_override)
    payload["source"] = _merge_source_metadata(
        payload["source"],
        row=row,
        origin="active_sheet_strategy",
        extra_metadata={
            "strategy_id": strategy_id,
            "catalog_symbol": entry.symbol,
            **_google_catalog_metadata(google_catalog_entry),
        },
    )
    return DeploymentManifest.model_validate(payload)


def _compile_manual_trigger_row(row: ActivePlanSheetRow) -> DeploymentManifest:
    stop_loss_pct = row.stop_loss_pct if row.stop_loss_pct is not None else 0.45
    hard_flat_time_et = row.hard_flat_time_et or "15:55"
    use_profit_target = row.use_profit_target if row.use_profit_target is not None else row.profit_target_multiple is not None

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


def _google_catalog_entry_payload(entry: StrategyCatalogSheetRow) -> dict[str, Any]:
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

    stop_loss_pct = _coerce_float(catastrophe_exit_params.get("stop_loss_pct")) or 0.45
    hard_flat_time_et = normalize_time_text(str(catastrophe_exit_params.get("hard_flat_time_et") or "15:55")) or "15:55"
    use_profit_target = bool(catastrophe_exit_params.get("use_profit_target", False))
    profit_target_multiple = _coerce_float(catastrophe_exit_params.get("profit_target_multiple"))
    stop_to_breakeven_after_r_multiple = _coerce_float(
        catastrophe_exit_params.get("stop_to_breakeven_after_r_multiple")
    )
    strategy_key = str(entry.strategy_key or "").strip()
    execution_payload: dict[str, Any] = {
        "profile": str(vehicle_mapping.get("profile") or "single_leg_long_premium_v1"),
        "shadow_only": True,
        "option_mapping": vehicle_mapping.get("option_mapping") or _option_mapping_from_structure(vehicle_mapping.get("structure")),
        "min_open_interest": _coerce_int(vehicle_mapping.get("min_open_interest")) or 100,
        "max_bid_ask_spread_pct": _coerce_float(vehicle_mapping.get("max_bid_ask_spread_pct")) or 0.20,
    }
    dte_range = _compact_numeric_range_text(vehicle_mapping.get("dte") or vehicle_mapping.get("dte_target"))
    if dte_range is not None:
        execution_payload["dte"] = dte_range
    else:
        execution_payload["dte_min"] = _coerce_int(vehicle_mapping.get("dte_min")) or 0
        execution_payload["dte_max"] = _coerce_int(vehicle_mapping.get("dte_max")) or 7
    delta_range = _compact_numeric_range_text(vehicle_mapping.get("delta_target") or vehicle_mapping.get("delta_plan"))
    if delta_range is not None:
        execution_payload["delta_target"] = delta_range
    else:
        execution_payload["target_abs_delta_min"] = _coerce_float(vehicle_mapping.get("target_abs_delta_min"))
        execution_payload["target_abs_delta_max"] = _coerce_float(vehicle_mapping.get("target_abs_delta_max"))
    entry_window = vehicle_mapping.get("entry_window_et")
    if entry_window is not None:
        execution_payload["entry_window_et"] = entry_window

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
        "risk": {
            "profile": f"{strategy_key}_risk_v1" if strategy_key else "catalog_promoted_v1",
            "max_trade_premium_usd": 300.0,
            "hard_flat_time_et": hard_flat_time_et,
            "stop_loss_pct": stop_loss_pct,
        },
        "exit": {
            "profile": f"{strategy_key}_exit_v1" if strategy_key else "catalog_promoted_exit_v1",
            "use_algorithmic_exit": strategy_key in NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS,
            "use_profit_target": use_profit_target,
            "profit_target_multiple": profit_target_multiple,
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
    if row.use_profit_target is not None:
        updated["use_profit_target"] = row.use_profit_target
    if row.profit_target_multiple is not None:
        updated["profit_target_multiple"] = row.profit_target_multiple
    if row.stop_loss_pct is not None:
        updated["stop_loss_pct"] = row.stop_loss_pct
    if row.hard_flat_time_et is not None:
        updated["hard_flat_time_et"] = row.hard_flat_time_et
    if row.stop_to_breakeven_after_r_multiple is not None:
        updated["stop_to_breakeven_after_r_multiple"] = row.stop_to_breakeven_after_r_multiple
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
    return (
        entry.bhiksha_ready
        and entry.lifecycle_status in {"active", "candidate"}
        and bool(entry.catalog_key)
        and bool(entry.symbol)
        and strategy_key in supported_keys
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
) -> None:
    if not google_entry.bhiksha_ready:
        raise ValueError(f"Strategy {strategy_id!r} is not bhiksha_ready in the Google strategy catalog")
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
    "flat_time": "hard_flat_time_et",
    "hard_flat": "hard_flat_time_et",
    "start": "entry_window_start_et",
    "end": "entry_window_end_et",
    "strategy_params": "strategy_params_override",
    "strategy_overrides": "strategy_params_override",
    "execution": "execution_overrides",
    "risk": "risk_overrides",
    "exit": "exit_overrides",
    "metadata": "source_metadata",
}
