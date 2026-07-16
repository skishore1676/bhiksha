"""Inspect or explicitly re-promote Rail B deployments while Bhiksha is stopped."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.tools.runtime_control_lock import runtime_control_lock
from bhiksha.tools.server_session import _runtime_status


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIRMATION = "REPROMOTE"
_RUNTIME_COMMAND_MARKERS = (
    "bhiksha.tools.trade_session",
    "bhiksha.tools.dry_run_live_loop",
    "bhiksha.tools.bionic_session",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List active demotions and evidence resets")

    repromote = subparsers.add_parser(
        "repromote",
        help="Remove active demotions and start fresh Rail B evidence windows",
    )
    repromote.add_argument("--deployment-id", action="append", required=True)
    repromote.add_argument("--reason", required=True)
    repromote.add_argument("--approved-by", required=True)
    repromote.add_argument(
        "--pid-path",
        type=Path,
        default=Path(
            os.getenv(
                "BHIKSHA_RUNTIME_PID_PATH",
                _REPO_ROOT / "artifacts/playbook/runtime/bhiksha.pid",
            )
        ),
    )
    repromote.add_argument(
        "--confirm-live-state-change",
        required=True,
        help=f"Must be exactly {_CONFIRMATION}",
    )
    args = parser.parse_args(argv)

    try:
        payload = _run(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _run(args: argparse.Namespace) -> dict[str, Any]:
    store = DemotionStore(args.store)
    if args.command == "list":
        return {
            "status": "ok",
            "store": str(store.path.resolve()),
            "active_demotions": {
                deployment_id: record.to_dict()
                for deployment_id, record in sorted(store.load().items())
            },
            "repromotion_history": {
                deployment_id: [record.to_dict() for record in history]
                for deployment_id, history in sorted(store.load_repromotion_history().items())
            },
            "sha256": _file_sha256(store.path),
        }

    if args.confirm_live_state_change != _CONFIRMATION:
        raise ValueError(f"--confirm-live-state-change must be exactly {_CONFIRMATION}")
    with runtime_control_lock(args.pid_path) as lock_path:
        try:
            runtime = _runtime_status(args.pid_path)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot prove runtime is stopped: {exc}") from exc
        process_pids = _runtime_process_pids()
        if runtime.get("running") or process_pids:
            raise RuntimeError(
                "Bhiksha runtime is running; stop it before re-promotion "
                f"(pid_file_pid={runtime.get('pid')}, process_scan_pids={process_pids})"
            )
        runtime = {
            **runtime,
            "control_lock_path": str(lock_path),
            "process_scan_pids": process_pids,
            "stopped_proof": "control_lock_and_pid_process_scan_clear",
        }

        before = store.load()
        before_sha256 = _file_sha256(store.path)
        changed = store.repromote_many(
            args.deployment_id,
            reason=args.reason,
            approved_by=args.approved_by,
        )
        after = store.load()
    return {
        "status": "ok",
        "action": "repromote",
        "runtime": runtime,
        "store": str(store.path.resolve()),
        "before_active_demotion_ids": sorted(before),
        "after_active_demotion_ids": sorted(after),
        "repromotions": {
            deployment_id: record.to_dict()
            for deployment_id, record in sorted(changed.items())
        },
        "before_sha256": before_sha256,
        "after_sha256": _file_sha256(store.path),
    }


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_process_pids() -> list[int]:
    """Return live Bhiksha trade-session PIDs independent of PID-file health."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    pids = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not any(
            marker in fields[1] for marker in _RUNTIME_COMMAND_MARKERS
        ):
            continue
        try:
            pids.append(int(fields[0]))
        except ValueError:
            continue
    return sorted(set(pids))


if __name__ == "__main__":
    raise SystemExit(main())
