"""Capture databases before session-check analysis; analysis uses only the copies."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from bhiksha.ops.monitoring_snapshot import DEFAULT_TIMEOUT_SECONDS, capture_monitoring_snapshot


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New directory, never an existing snapshot or runtime checkout")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="Per-database copy budget; validation follows after source closure")
    parser.add_argument("--deadline-seconds", type=float,
                        help="Owner total budget including copy, validation and hashing")
    args = parser.parse_args(argv)
    if sys.platform == "darwin":
        # A lock-safe copy still competes for disk and CPU. Give the runtime
        # priority over this optional monitoring process and its integrity scan.
        subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(os.getpid())],
                       check=True, capture_output=True)
    options = {"timeout_seconds": args.timeout_seconds}
    if args.deadline_seconds is not None:
        options["deadline_seconds"] = args.deadline_seconds
    result = capture_monitoring_snapshot(args.root, args.output_dir, **options)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
