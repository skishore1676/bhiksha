"""Compile an operator sheet export into Bhiksha's active plan."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bhiksha.active_plan.compiler import compile_active_plan_from_google_sheets, write_compiled_active_plan
from bhiksha.config.environment import get_mala_evidence_sheet_name, get_operator_defaults_sheet_name, load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    default_google_sheet_id = os.getenv("GOOGLE_SHEET_ID")
    default_credentials_path = os.getenv("GOOGLE_API_CREDENTIALS_PATH")
    default_catalog_sheet_name = get_mala_evidence_sheet_name()
    default_defaults_sheet_name = get_operator_defaults_sheet_name()
    default_strategy_sheet_name = os.getenv("ACTIVE_STRATEGIES_SHEET_NAME", "active_strategies")
    default_manual_sheet_name = os.getenv("MANUAL_ENTRY_SHEET_NAME") or os.getenv("MANNUAL_ENTRY_SHEET_NAME") or "manual_entry"
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=default_google_sheet_id is None)
    input_group.add_argument("--sheet", help="Path to the exported operator sheet (.csv or .json)")
    input_group.add_argument("--google-sheet-id", default=None, help="Google spreadsheet URL or ID for the active control plane")
    parser.add_argument(
        "--strategy-catalog",
        default="config/strategy_catalog",
        help="Path to the approved Bhiksha strategy catalog directory",
    )
    parser.add_argument(
        "--credentials-path",
        default=default_credentials_path,
        help="Google service-account credentials JSON path. Required with --google-sheet-id.",
    )
    parser.add_argument("--catalog-sheet-name", default=default_catalog_sheet_name, help="Worksheet name for Mala_Evidence_v1")
    parser.add_argument("--defaults-sheet-name", default=default_defaults_sheet_name, help="Worksheet name for operator defaults")
    parser.add_argument("--strategy-sheet-name", default=default_strategy_sheet_name, help="Worksheet name for approved strategy activations")
    parser.add_argument("--manual-sheet-name", default=default_manual_sheet_name, help="Worksheet name for manual trade setups")
    parser.add_argument(
        "--out",
        default="artifacts/playbook/active_plan.json",
        help="Where to write the compiled active plan JSON",
    )
    parser.add_argument("--active-plan-id", default=None, help="Optional explicit active_plan_id")
    parser.add_argument("--trading-date", default=None, help="Optional trading date in YYYY-MM-DD format")
    parser.add_argument("--source-name", default="google_sheet_integration", help="Recorded source.name for this plan")
    args = parser.parse_args(argv)
    google_sheet_id = args.google_sheet_id if args.google_sheet_id is not None else (None if args.sheet else default_google_sheet_id)

    if google_sheet_id:
        if not args.credentials_path:
            parser.error("--credentials-path is required with --google-sheet-id")
        compiled = compile_active_plan_from_google_sheets(
            spreadsheet_id=google_sheet_id,
            credentials_path=args.credentials_path,
            catalog_sheet_name=args.catalog_sheet_name,
            defaults_sheet_name=args.defaults_sheet_name,
            strategy_sheet_name=args.strategy_sheet_name,
            manual_sheet_name=args.manual_sheet_name,
            strategy_catalog_path=args.strategy_catalog,
            active_plan_id=args.active_plan_id,
            trading_date=args.trading_date,
            source_name=args.source_name,
        )
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(compiled.plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        plan = compiled.plan
    else:
        plan = write_compiled_active_plan(
            sheet_path=args.sheet,
            strategy_catalog_path=args.strategy_catalog,
            output_path=args.out,
            active_plan_id=args.active_plan_id,
            trading_date=args.trading_date,
            source_name=args.source_name,
        )
    print(f"ACTIVE_PLAN={Path(args.out).resolve()}")
    print(f"ACTIVE_PLAN_ID={plan.active_plan_id}")
    print("SUMMARY=" + json.dumps(plan.summary, sort_keys=True))
    print(f"SUPPRESSED_COUNT={len(plan.suppressed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
