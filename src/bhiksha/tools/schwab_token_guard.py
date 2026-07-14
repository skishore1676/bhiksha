"""Premarket Schwab token guard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex

from bhiksha.config.environment import load_dotenv
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.ops.schwab_token_guard import run_schwab_token_guard_sync


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="premarket", choices=["premarket", "after_close", "startup", "manual"])
    parser.add_argument("--browser-renewal-mode", default="off", choices=["off", "auto", "force"])
    parser.add_argument("--browser-renewal-cmd", default=None, help="Shell command used to invoke browser renewal")
    parser.add_argument(
        "--refresh-lead-days",
        type=float,
        default=None,
        help="Optional extra wall-clock lead; the default is next-trading-session aware.",
    )
    parser.add_argument("--receipt-dir", default="artifacts/playbook/schwab_token_guard")
    parser.add_argument("--no-receipt", action="store_true")
    parser.add_argument("--alert-mode", default="live", choices=["off", "spool", "live"])
    parser.add_argument("--alert-profile", default=None)
    parser.add_argument("--alert-cmd", default=None, help="Shell command used to invoke lathi-bus")
    parser.add_argument("--alert-cwd", default=None, help="Working directory for the lathi-bus command")
    parser.add_argument("--json", action="store_true", help="Print the full structured result")
    args = parser.parse_args(argv)

    command = ["/bin/bash", "-lc", args.browser_renewal_cmd] if args.browser_renewal_cmd else None
    alert_command = shlex.split(args.alert_cmd) if args.alert_cmd else None
    result = run_schwab_token_guard_sync(
        settings=SchwabSettings.from_env(),
        mode=args.mode,
        browser_renewal_mode=args.browser_renewal_mode,
        browser_renewal_cmd=command,
        refresh_lead_days=args.refresh_lead_days,
        receipt_dir=Path(args.receipt_dir),
        write_receipt=not args.no_receipt,
        alert_mode=args.alert_mode,
        alert_profile=args.alert_profile,
        alert_command=alert_command,
        alert_cwd=args.alert_cwd,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"SCHWAB_TOKEN_GUARD_OK={result.ok}")
        print(f"MODE={result.mode}")
        print(f"INITIAL_STATE={result.initial.state}")
        print(f"FINAL_STATE={result.final.state}")
        print(f"ACTION={result.action}")
        print(f"DIRECT_REFRESH_ATTEMPTED={result.direct_refresh_attempted}")
        print(f"DIRECT_REFRESH_OK={result.direct_refresh_ok}")
        print(f"BROWSER_RENEWAL_INVOKED={result.browser.invoked}")
        if result.browser.return_code is not None:
            print(f"BROWSER_RENEWAL_RC={result.browser.return_code}")
        print(f"ALERT_ATTEMPTED={result.alert.attempted}")
        print(f"ALERT_OK={result.alert.ok}")
        if result.receipt_path:
            print(f"RECEIPT={result.receipt_path}")
        if result.error:
            print(f"ERROR={result.error}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
