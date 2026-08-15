"""Project a frozen Cartographer signal batch into manual_entry A:V rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.cartographer_profiles import canonical_hash
from bhiksha.integrations.cartographer_projector import project_with_table
from bhiksha.integrations.google_sheets import GoogleSheetTableClient


def run_projection(*, table, signal_batch: Path, trading_date: str, premium_ceiling: float, apply: bool) -> dict:
    payload = json.loads(signal_batch.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("signal batch must be a JSON object")
    return project_with_table(
        table, payload, operator_premium_ceiling=premium_ceiling,
        trading_date=trading_date, apply=apply,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-batch", type=Path, required=True)
    parser.add_argument("--trading-date", required=True)
    parser.add_argument("--premium-ceiling", type=float, required=True)
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-name", default="manual_entry")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="perform the otherwise dry-run Sheet update")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    batch = json.loads(args.signal_batch.expanduser().read_text(encoding="utf-8"))
    try:
        table = GoogleSheetTableClient(args.spreadsheet_id, args.sheet_name, args.credentials)
        receipt = run_projection(table=table, signal_batch=args.signal_batch, trading_date=args.trading_date, premium_ceiling=args.premium_ceiling, apply=args.apply)
    except Exception as exc:
        body = {
            "schema": "bhiksha.cartographer_projection_receipt.v1",
            "status": "failed",
            "producer_run_id": batch.get("run_id") if isinstance(batch, dict) else None,
            "signal_batch_hash": batch.get("signal_batch_hash") if isinstance(batch, dict) else None,
            "trading_date": args.trading_date,
            "apply_requested": args.apply,
            "error": f"{type(exc).__name__}:{exc}",
            "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
        }
        receipt = {**body, "receipt_hash": canonical_hash(body)}
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 2
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
