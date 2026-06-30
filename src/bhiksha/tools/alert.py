"""Send a Bhiksha-owned operator alert through Lathi Bus."""

from __future__ import annotations

import argparse
import json
import shlex

from bhiksha.config.environment import load_dotenv
from bhiksha.ops.alerts import send_lathi_alert


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--level", default="info")
    parser.add_argument("--mode", default="live", choices=["spool", "live"])
    parser.add_argument("--profile", default=None)
    parser.add_argument("--alert-cmd", default=None, help="Shell command used to invoke lathi-bus")
    parser.add_argument("--alert-cwd", default=None, help="Working directory for the lathi-bus command")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    command = shlex.split(args.alert_cmd) if args.alert_cmd else None
    result = send_lathi_alert(
        title=args.title,
        body=args.body,
        level=args.level,
        mode=args.mode,
        profile=args.profile,
        command=command,
        cwd=args.alert_cwd,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"ALERT_ATTEMPTED={result.attempted}")
        print(f"ALERT_OK={result.ok}")
        print(f"ALERT_MODE={result.mode}")
        if result.return_code is not None:
            print(f"ALERT_RC={result.return_code}")
        if result.error:
            print(f"ERROR={result.error}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
