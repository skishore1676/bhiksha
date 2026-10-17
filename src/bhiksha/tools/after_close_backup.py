"""Run the existing after-close owner's backup/recovery path, never trading."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from bhiksha.ops.after_close_backup import run_backup, snapshot_manifest, make_archive, verify_restore, read_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--session", type=date.fromisoformat)
    parser.add_argument("--snapshot", type=Path, help="Reuse a complete after-close monitoring snapshot")
    parser.add_argument("--worker", choices=["manifest", "archive", "restore"], help=argparse.SUPPRESS)
    parser.add_argument("--cutoff")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    os.nice(10)
    if args.worker:
        if sys.platform == "darwin":
            subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(os.getpid())], check=True, capture_output=True)
        if args.worker == "manifest":
            result = snapshot_manifest(args.snapshot, datetime.fromisoformat(args.cutoff))
        elif args.worker == "archive":
            result = {"path": str(make_archive(args.snapshot, args.bundle.parent, read_json(args.bundle)))}
        else:
            result = verify_restore(args.archive, args.destination, read_json(args.bundle))
    else:
        result = run_backup(args.root, session=args.session, snapshot=args.snapshot)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
