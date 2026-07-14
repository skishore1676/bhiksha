"""Run the redacted Schwab account and market-data healthcheck."""

from __future__ import annotations

import argparse
import asyncio
import json

from bhiksha.ops.schwab_health import run_schwab_healthcheck


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", default=["QQQ", "IWM"])
    args = parser.parse_args(argv)
    result = asyncio.run(run_schwab_healthcheck(symbols=tuple(str(item).upper() for item in args.symbols)))
    print("SCHWAB_HEALTH=" + json.dumps(result.to_dict(), sort_keys=True))
    if result.ok:
        print("SCHWAB_HEALTH_OK")
        return 0
    print("SCHWAB_HEALTH_FAILED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
