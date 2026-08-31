#!/usr/bin/env python3
"""Close the PDD canary phase and retain its bounded continuing-live lane."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bhiksha.active_plan.compiler import (
    GoogleSheetTableClient,
    _compile_row,
    compile_active_plan_from_rows,
    compute_live_triage_authorization_sha256,
    load_operator_defaults_sheet_rows,
    load_rows_from_sheet_records_with_report,
    load_strategy_catalog_sheet_rows_with_report,
    sync_google_strategy_catalog,
)
from bhiksha.config.loader import load_strategy_catalog
from bhiksha.config.environment import load_dotenv


DEPLOYMENT_ID = "strategy_triage_market_impulse_pdd_pdd_long_live_row_29"
STRATEGY_ID = "triage-market_impulse-PDD__pdd_long"
CANARY_CONTRACT = "pdd-entry-canary.v2"
CONTINUING_CONTRACT = "pdd-entry-live.v1"
SHEET_NAMES = (
    "active_strategy",
    "manual_entry",
    "Mala_Evidence_v1",
    "Operator_Defaults_v1",
)


def _canonical_sha(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_object(value: object, *, field: str) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PDD row has invalid {field}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"PDD row {field} must be a JSON object")
    return parsed


def _read_snapshot(clients: dict[str, GoogleSheetTableClient]) -> dict[str, list[dict]]:
    first = {name: clients[name].read_rows() for name in SHEET_NAMES}
    second = {name: clients[name].read_rows() for name in SHEET_NAMES}
    changed = [name for name in SHEET_NAMES if _canonical_sha(first[name]) != _canonical_sha(second[name])]
    if changed:
        raise RuntimeError("Sheet source drift across reads: " + ", ".join(changed))
    return first


def _find_pdd_row(rows: list[dict]) -> dict:
    matches = [row for row in rows if row.get("strategy_id") == STRATEGY_ID]
    if len(matches) != 1:
        raise RuntimeError(f"expected one PDD active_strategy row, found {len(matches)}")
    return matches[0]


def _assert_preimage(row: dict) -> None:
    if row.get("enabled") not in {True, "TRUE"}:
        raise RuntimeError("PDD row is not enabled")
    if str(row.get("authorization_mode") or "").lower() != "live":
        raise RuntimeError("PDD row is not live-authorized")
    if float(row.get("max_trade_premium_usd") or 0) != 1_000.0:
        raise RuntimeError("PDD premium cap drifted from 1000")
    if int(float(row.get("max_contracts") or 0)) != 2:
        raise RuntimeError("PDD contract cap drifted from 2")
    metadata = _json_object(row.get("source_metadata"), field="source_metadata")
    if metadata.get("authorization_contract_version") != CANARY_CONTRACT:
        raise RuntimeError("PDD row is not the expected v2 canary preimage")
    if metadata.get("authorized_deployment_id") != DEPLOYMENT_ID:
        raise RuntimeError("PDD deployment identity drifted")


def _parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--spreadsheet-id", default=os.getenv("GOOGLE_SHEET_ID"))
    parser.add_argument(
        "--credentials",
        type=Path,
        default=os.getenv("GOOGLE_API_CREDENTIALS_PATH"),
    )
    parser.add_argument(
        "--active-plan",
        type=Path,
        default=Path("artifacts/playbook/active_plan.json"),
    )
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path("artifacts/playbook/releases/pdd_live_graduation_20260831.json"),
    )
    args = parser.parse_args()
    if not args.spreadsheet_id:
        parser.error("--spreadsheet-id or GOOGLE_SHEET_ID is required")
    if not args.credentials:
        parser.error("--credentials or GOOGLE_API_CREDENTIALS_PATH is required")
    return args


def main() -> None:
    args = _parse_args()
    active_plan = json.loads(args.active_plan.read_text(encoding="utf-8"))
    active_plan_id = str(active_plan["active_plan_id"])
    now = datetime.now(ZoneInfo("America/Chicago")).replace(microsecond=0)
    trading_date = now.date().isoformat()
    clients = {
        name: GoogleSheetTableClient(
            spreadsheet_id=args.spreadsheet_id,
            sheet_name=name,
            credentials_path=args.credentials,
        )
        for name in SHEET_NAMES
    }
    snapshot = _read_snapshot(clients)
    observed_row = copy.deepcopy(_find_pdd_row(snapshot["active_strategy"]))
    _assert_preimage(observed_row)
    preimage_sha = _canonical_sha(observed_row)

    projected_rows = copy.deepcopy(snapshot["active_strategy"])
    projected_row = _find_pdd_row(projected_rows)
    metadata = _json_object(projected_row.get("source_metadata"), field="source_metadata")
    metadata.update(
        {
            "authorization_contract_version": CONTINUING_CONTRACT,
            "experiment_status": "closed",
            "experiment_closed_at": now.isoformat(),
            "continuing_live_authorized_by": args.approved_by,
            "continuing_live_authorized_at": now.isoformat(),
            "continuing_live_reason": "retain bounded PDD live observation after canary completion",
            "authorization_sha256": "0" * 64,
        }
    )
    projected_row["notes"] = (
        "PDD canary closed 2026-08-31; continuing live authorized by Suman at "
        "maximum two contracts and $1,000 premium with TREND_CONTINUATION, "
        "Risk Envelope off, frozen 0-3 DTE, and existing fail-closed stop policy."
    )
    projected_row["source_metadata"] = json.dumps(metadata, sort_keys=True, separators=(",", ":"))

    catalog_report = load_strategy_catalog_sheet_rows_with_report(
        snapshot["Mala_Evidence_v1"],
        sheet_name="Mala_Evidence_v1",
    )
    if catalog_report.suppressed:
        raise RuntimeError(f"suppressed Mala evidence rows: {catalog_report.suppressed}")
    operator_defaults = load_operator_defaults_sheet_rows(snapshot["Operator_Defaults_v1"])

    with tempfile.TemporaryDirectory(prefix="pdd-continuing-live-") as temp_dir:
        catalog_root = Path(temp_dir) / "catalog"
        sync_google_strategy_catalog(
            strategy_catalog_path=catalog_root,
            google_strategy_catalog=catalog_report.rows,
            operator_defaults=operator_defaults,
        )
        local_by_id = {item.strategy_id: item for item in load_strategy_catalog(catalog_root)}
        google_by_id = {item.catalog_key: item for item in catalog_report.rows}
        strategy_report = load_rows_from_sheet_records_with_report(
            projected_rows,
            row_type="strategy",
            sheet_name="active_strategy",
        )
        if strategy_report.suppressed:
            raise RuntimeError(f"suppressed strategy rows: {strategy_report.suppressed}")
        compiled_row = next(row for row in strategy_report.rows if row.strategy_id == STRATEGY_ID)
        provisional = _compile_row(compiled_row, local_by_id, google_by_id, True)
        metadata["authorization_sha256"] = compute_live_triage_authorization_sha256(
            provisional,
            active_plan_id=active_plan_id,
        )
        projected_row["source_metadata"] = json.dumps(metadata, sort_keys=True, separators=(",", ":"))

        strategy_report = load_rows_from_sheet_records_with_report(
            projected_rows,
            row_type="strategy",
            sheet_name="active_strategy",
        )
        manual_report = load_rows_from_sheet_records_with_report(
            snapshot["manual_entry"],
            row_type="manual",
            sheet_name="manual_entry",
        )
        if strategy_report.suppressed or manual_report.suppressed:
            raise RuntimeError(
                f"suppressed rows after authorization: strategy={strategy_report.suppressed}, "
                f"manual={manual_report.suppressed}"
            )
        compiled = compile_active_plan_from_rows(
            rows=[*strategy_report.rows, *manual_report.rows],
            strategy_catalog_path=catalog_root,
            active_plan_id=active_plan_id,
            trading_date=trading_date,
            source_name="pdd_continuing_live_authorization",
            google_strategy_catalog=catalog_report.rows,
            operator_defaults=operator_defaults,
        )
        deployment = next(item for item in compiled.plan.deployments if item.deployment_id == DEPLOYMENT_ID)
        if deployment.risk.max_contracts != 2 or deployment.risk.max_trade_premium_usd != 1_000.0:
            raise RuntimeError("PDD continuing-live size changed unexpectedly")
        inhibition_warnings = [
            warning
            for warning in compiled.plan.summary.get("canary_inhibition_warnings", [])
            if warning.get("deployment_id") == DEPLOYMENT_ID
        ]

    result = {
        "schema": "bhiksha.pdd_continuing_live_graduation.v1",
        "generated_at": now.isoformat(),
        "approved_by": args.approved_by,
        "effect": "sheet_update" if args.apply else "none",
        "deployment_id": DEPLOYMENT_ID,
        "active_plan_id": active_plan_id,
        "trading_date": trading_date,
        "preimage_sha256": preimage_sha,
        "postimage_sha256": _canonical_sha(projected_row),
        "authorization_sha256": metadata["authorization_sha256"],
        "contract_before": CANARY_CONTRACT,
        "contract_after": CONTINUING_CONTRACT,
        "experiment_status": "closed",
        "continuing_live_authorized": True,
        "effective_live": not deployment.execution.shadow_only,
        "inhibition_warnings": inhibition_warnings,
        "max_contracts": 2,
        "max_trade_premium_usd": 1_000.0,
    }

    if args.apply:
        current_row = _find_pdd_row(clients["active_strategy"].read_rows())
        if _canonical_sha(current_row) != preimage_sha:
            raise RuntimeError("PDD Sheet row changed after preview; refusing update")
        clients["active_strategy"].update_row_cells(
            row_index=int(current_row["row_index"]),
            values={
                "notes": projected_row["notes"],
                "source_metadata": projected_row["source_metadata"],
            },
        )
        readback = _find_pdd_row(clients["active_strategy"].read_rows())
        if _canonical_sha(readback) != _canonical_sha(projected_row):
            raise RuntimeError("PDD Sheet readback does not match authorized postimage")
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
