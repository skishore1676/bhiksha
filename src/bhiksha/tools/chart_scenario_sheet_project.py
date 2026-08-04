"""Apply a validated TradeLab request to only Chart_Scenarios_v1."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bhiksha.chart_scenarios.validation import read_installed_plan
from bhiksha.config.environment import load_dotenv
from bhiksha.integrations.google_sheets import GoogleSheetTableClient
from bhiksha.ops.chart_scenario_sheet import (
    SHEET_NAME,
    project_sheet_upsert_request,
    validate_sheet_upsert_request,
)


def main(argv: list[str] | None = None) -> int:
    if os.getenv("BHIKSHA_SANITIZED_SUBPROCESS") != "1":
        load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--credentials",
        default=_credentials_path(),
    )
    args = parser.parse_args(argv)
    if not args.credentials:
        raise ValueError("Google Sheets credentials path is required")
    plan = read_installed_plan(args.plan)
    request = validate_sheet_upsert_request(
        json.loads(Path(args.request).read_text(encoding="utf-8")), plan=plan
    )
    client = GoogleSheetTableClient(
        spreadsheet_id=request["spreadsheet_id"],
        sheet_name=SHEET_NAME,
        credentials_path=Path(args.credentials),
    )
    receipt = project_sheet_upsert_request(
        request,
        client=client,
        receipt_path=args.receipt,
        plan=plan,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


def _credentials_path() -> str | None:
    """Prefer the lane override, then Bhiksha's existing canonical credential."""

    return os.getenv("BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH") or os.getenv(
        "GOOGLE_API_CREDENTIALS_PATH"
    )


if __name__ == "__main__":
    raise SystemExit(main())
