"""Operator wrapper for Bhiksha active-plan prepare, run, and review flows."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from bhiksha.app.bootstrap import build_runtime
from bhiksha.active_plan.compiler import compile_active_plan_from_google_sheets, write_compiled_active_plan
from bhiksha.bionic.session_ops import (
    default_active_plan_path,
    default_feedback_export_dir,
    default_mala_root,
    default_prepare_out_dir,
    parse_compile_stdout,
    run_compile_command,
    session_summary_to_dict,
    write_feedback_bundle,
)
from bhiksha.config.environment import get_mala_evidence_sheet_name, load_dotenv
from bhiksha.loop.observation import write_observation_reports
from bhiksha.ops.summary import build_session_summary
from bhiksha.tools.healthcheck import _run as run_healthcheck
from bhiksha.tools.trade_session import main as trade_session_main


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    default_google_sheet_id = os.getenv("GOOGLE_SHEET_ID")
    default_credentials_path = os.getenv("GOOGLE_API_CREDENTIALS_PATH")
    default_catalog_sheet_name = get_mala_evidence_sheet_name()
    default_defaults_sheet_name = os.getenv("OPERATOR_DEFAULTS_SHEET_NAME")
    default_strategy_sheet_name = os.getenv("ACTIVE_STRATEGIES_SHEET_NAME", "active_strategies")
    default_manual_sheet_name = os.getenv("MANUAL_ENTRY_SHEET_NAME") or os.getenv("MANNUAL_ENTRY_SHEET_NAME") or "manual_entry"
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Prepare the current active plan from Google Sheets")
    prepare.add_argument("--sheet", default=None, help="Legacy fallback: path to a local sheet export (.csv or .json)")
    prepare.add_argument("--google-sheet-id", default=None, help="Google spreadsheet URL or ID for the active control plane")
    prepare.add_argument("--credentials-path", default=default_credentials_path, help="Google service-account credentials JSON path")
    prepare.add_argument("--catalog-sheet-name", default=default_catalog_sheet_name, help="Worksheet name for Mala_Evidence_v1")
    prepare.add_argument("--defaults-sheet-name", default=default_defaults_sheet_name, help="Optional worksheet name for operator defaults")
    prepare.add_argument("--strategy-sheet-name", default=default_strategy_sheet_name, help="Worksheet name for approved strategy activations")
    prepare.add_argument("--manual-sheet-name", default=default_manual_sheet_name, help="Worksheet name for manual trade setups")
    prepare.add_argument(
        "--strategy-catalog",
        default="config/strategy_catalog",
        help="Path to the approved Bhiksha strategy catalog",
    )
    prepare.add_argument(
        "--active-plan-out",
        default=None,
        help="Where to write the compiled active plan when using --sheet",
    )
    prepare.add_argument("--active-plan-id", default=None, help="Optional explicit active_plan_id when using --sheet")
    prepare.add_argument("--trading-date", default=None, help="Optional trading date in YYYY-MM-DD format when using --sheet")
    prepare.add_argument("--source-name", default="google_sheet_integration", help="Recorded source.name when using --sheet")
    prepare.add_argument("--mala-root", default=None, help="Legacy fallback: path to the sibling Mala repo")
    prepare.add_argument("--mala-python", default=None, help="Legacy fallback: optional Python executable for Mala")
    prepare.add_argument("--out-dir", default=None, help="Legacy fallback: where Mala should write compiled plan artifacts")
    prepare.add_argument("--shadow-authorized", action="store_true", help="Legacy fallback: keep execution.shadow_only=true in the exported plan")
    prepare.add_argument("--skip-healthcheck", action="store_true")
    prepare.add_argument("--skip-warm-start", action="store_true")
    prepare.add_argument("--start-live", action="store_true", help="After prepare checks pass, start the live Bhiksha session")
    prepare.add_argument("extra_compile_args", nargs=argparse.REMAINDER, help="Legacy fallback args passed through to Mala compile_active_session.py")

    run = subparsers.add_parser("run", help="Run Bhiksha against the current active plan")
    run.add_argument("--active-plan", default=None, help="Path to active_plan.json")
    run.add_argument("--live", action="store_true")
    run.add_argument("--max-bars", type=int, default=None)

    review = subparsers.add_parser("review", help="Generate post-close feedback and optionally export it back to Mala")
    review.add_argument("--active-plan", default=None, help="Path to active_plan.json")
    review.add_argument("--db-path", default=None, help="Override SQLite DB path")
    review.add_argument("--recent", type=int, default=20)
    review.add_argument("--trading-days", type=int, default=3)
    review.add_argument("--provider", default=None)
    review.add_argument("--skip-replay", action="store_true")
    review.add_argument("--mala-root", default=None, help="Path to the sibling Mala repo for feedback export")
    review.add_argument("--no-export-to-mala", action="store_true")
    return parser


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _prepare(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    google_sheet_id = args.google_sheet_id if args.google_sheet_id is not None else (None if args.sheet else os.getenv("GOOGLE_SHEET_ID"))
    active_plan_inputs = sum(1 for value in (args.sheet, google_sheet_id) if value)
    if active_plan_inputs > 1:
        raise ValueError("Specify only one active-plan input source: --sheet or --google-sheet-id")

    if args.sheet or google_sheet_id:
        active_plan_path = Path(args.active_plan_out).resolve() if args.active_plan_out else default_active_plan_path(repo_root)
        strategy_catalog = Path(args.strategy_catalog)
        if not strategy_catalog.is_absolute():
            strategy_catalog = (repo_root / strategy_catalog).resolve()
        if google_sheet_id:
            credentials_path = args.credentials_path or os.getenv("GOOGLE_API_CREDENTIALS_PATH")
            if not credentials_path:
                raise ValueError("--credentials-path or GOOGLE_API_CREDENTIALS_PATH is required with --google-sheet-id")
            compiled = compile_active_plan_from_google_sheets(
                spreadsheet_id=google_sheet_id,
                credentials_path=credentials_path,
                catalog_sheet_name=args.catalog_sheet_name,
                defaults_sheet_name=args.defaults_sheet_name,
                strategy_sheet_name=args.strategy_sheet_name,
                manual_sheet_name=args.manual_sheet_name,
                strategy_catalog_path=strategy_catalog,
                active_plan_id=args.active_plan_id,
                trading_date=args.trading_date,
                source_name=args.source_name,
            )
            active_plan_path.parent.mkdir(parents=True, exist_ok=True)
            active_plan_path.write_text(
                json.dumps(compiled.plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            plan = compiled.plan
        else:
            plan = write_compiled_active_plan(
                sheet_path=Path(args.sheet).resolve(),
                strategy_catalog_path=strategy_catalog,
                output_path=active_plan_path,
                active_plan_id=args.active_plan_id,
                trading_date=args.trading_date,
                source_name=args.source_name,
            )
        active_plan = str(active_plan_path)
        print(f"ACTIVE_PLAN={active_plan}")
        print(f"ACTIVE_PLAN_ID={plan.active_plan_id}")
        print("SUMMARY=" + json.dumps(plan.summary, sort_keys=True))
    else:
        mala_root = Path(args.mala_root).resolve() if args.mala_root else default_mala_root(repo_root)
        out_dir = Path(args.out_dir).resolve() if args.out_dir else default_prepare_out_dir(mala_root)
        extra_compile_args = list(args.extra_compile_args or [])
        if extra_compile_args and extra_compile_args[0] == "--":
            extra_compile_args = extra_compile_args[1:]

        result = run_compile_command(
            mala_root=mala_root,
            out_dir=out_dir,
            live_authorized=not args.shadow_authorized,
            publish_bhiksha=True,
            mala_python=args.mala_python,
            extra_args=extra_compile_args,
        )
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        parsed = parse_compile_stdout(result.stdout)
        active_plan = (
            parsed.get("BHIKSHA_ACTIVE_PLAN")
            or str(default_active_plan_path(repo_root))
        )

    if not args.skip_healthcheck:
        asyncio.run(run_healthcheck())
    if not args.skip_warm_start:
        trade_session_main(["--active-plan", active_plan, "--max-bars", "0"])
    if args.start_live:
        return trade_session_main(["--active-plan", active_plan, "--live"])
    return 0


def _run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    active_plan = args.active_plan or str(default_active_plan_path(repo_root))
    argv = ["--active-plan", active_plan]
    if args.live:
        argv.append("--live")
    if args.max_bars is not None:
        argv.extend(["--max-bars", str(args.max_bars)])
    return trade_session_main(argv)


async def _review_async(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    active_plan = Path(args.active_plan).resolve() if args.active_plan else default_active_plan_path(repo_root)
    runtime = build_runtime(active_plan_path=active_plan)
    db_path = Path(args.db_path) if args.db_path else Path(runtime.app_config.sqlite_path)
    if not db_path.is_absolute():
        db_path = repo_root / db_path
    summary = build_session_summary(str(db_path), recent_limit=args.recent)
    observations_dir = repo_root / runtime.app_config.observation_reports_dir
    packets = await write_observation_reports(
        db_path=db_path,
        trading_days=args.trading_days,
        provider=args.provider,
        output_dir=observations_dir,
        include_replay=not args.skip_replay,
        active_plan_path=active_plan,
    )
    mala_root = None if args.no_export_to_mala else (Path(args.mala_root).resolve() if args.mala_root else default_mala_root(repo_root))
    bundle_dir = write_feedback_bundle(
        repo_root=repo_root,
        active_plan_path=active_plan,
        summary=summary,
        observation_packets=packets,
        export_to_mala_root=mala_root,
    )
    print(f"ACTIVE_PLAN={active_plan}")
    print(f"SESSION_FEEDBACK_DIR={bundle_dir}")
    if mala_root is not None:
        plan = json.loads(active_plan.read_text(encoding='utf-8'))
        active_plan_id = str(plan.get("active_plan_id") or active_plan.stem)
        print(f"MALA_FEEDBACK_DIR={default_feedback_export_dir(mala_root, active_plan_id)}")
    print("SESSION_SUMMARY_JSON=" + json.dumps(session_summary_to_dict(summary), sort_keys=True))
    print(f"OBSERVATION_COUNT={len(packets)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return _prepare(args)
    if args.command == "run":
        return _run(args)
    if args.command == "review":
        return asyncio.run(_review_async(args))
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
