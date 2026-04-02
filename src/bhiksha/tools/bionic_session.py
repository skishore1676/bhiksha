"""Canonical Bionic pre-open, run, and review workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from bhiksha.app.bootstrap import build_runtime
from bhiksha.bionic.session_ops import (
    default_feedback_export_dir,
    default_mala_root,
    default_prepare_out_dir,
    default_session_payload_path,
    parse_compile_stdout,
    run_compile_command,
    session_summary_to_dict,
    write_feedback_bundle,
)
from bhiksha.loop.observation import write_observation_reports
from bhiksha.ops.summary import build_session_summary
from bhiksha.tools.healthcheck import _run as run_healthcheck
from bhiksha.tools.trade_session import main as trade_session_main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Compile and publish the current active session")
    prepare.add_argument("--mala-root", default=None, help="Path to the sibling Mala repo")
    prepare.add_argument("--mala-python", default=None, help="Optional Python executable for Mala")
    prepare.add_argument("--out-dir", default=None, help="Where Mala should write active_session artifacts")
    prepare.add_argument("--shadow-authorized", action="store_true", help="Keep execution.shadow_only=true in the session payload")
    prepare.add_argument("--skip-healthcheck", action="store_true")
    prepare.add_argument("--skip-warm-start", action="store_true")
    prepare.add_argument("--start-live", action="store_true", help="After prepare checks pass, start the live Bhiksha session")
    prepare.add_argument("extra_compile_args", nargs=argparse.REMAINDER, help="Extra args passed through to Mala compile_active_session.py")

    run = subparsers.add_parser("run", help="Run Bhiksha against the active session payload")
    run.add_argument("--session-payload", default=None, help="Path to active_session.json")
    run.add_argument("--live", action="store_true")
    run.add_argument("--max-bars", type=int, default=None)

    review = subparsers.add_parser("review", help="Generate post-close feedback and export it back to Mala")
    review.add_argument("--session-payload", default=None, help="Path to active_session.json")
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
    session_payload = parsed.get("BHIKSHA_ACTIVE_SESSION") or str(default_session_payload_path(repo_root))

    if not args.skip_healthcheck:
        asyncio.run(run_healthcheck())
    if not args.skip_warm_start:
        trade_session_main(["--session-payload", session_payload, "--max-bars", "0"])
    if args.start_live:
        return trade_session_main(["--session-payload", session_payload, "--live"])
    return 0


def _run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    session_payload = args.session_payload or str(default_session_payload_path(repo_root))
    argv = ["--session-payload", session_payload]
    if args.live:
        argv.append("--live")
    if args.max_bars is not None:
        argv.extend(["--max-bars", str(args.max_bars)])
    return trade_session_main(argv)


async def _review_async(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    session_payload = Path(args.session_payload).resolve() if args.session_payload else default_session_payload_path(repo_root)
    runtime = build_runtime(session_payload_path=session_payload)
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
        session_payload_path=session_payload,
    )
    mala_root = None if args.no_export_to_mala else (Path(args.mala_root).resolve() if args.mala_root else default_mala_root(repo_root))
    bundle_dir = write_feedback_bundle(
        repo_root=repo_root,
        session_payload_path=session_payload,
        summary=summary,
        observation_packets=packets,
        export_to_mala_root=mala_root,
    )
    print(f"SESSION_PAYLOAD={session_payload}")
    print(f"SESSION_FEEDBACK_DIR={bundle_dir}")
    if mala_root is not None:
        session = json.loads(session_payload.read_text(encoding='utf-8'))
        session_id = str(session.get("session_id") or session_payload.stem)
        print(f"MALA_FEEDBACK_DIR={default_feedback_export_dir(mala_root, session_id)}")
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
