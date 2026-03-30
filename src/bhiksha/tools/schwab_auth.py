"""Manual Schwab auth helper.

Usage:
  PYTHONPATH=src .venv/bin/python -m bhiksha.tools.schwab_auth url
  PYTHONPATH=src .venv/bin/python -m bhiksha.tools.schwab_auth exchange "<returned callback url>"
"""

from __future__ import annotations

import asyncio
import sys

from bhiksha.integrations.schwab.auth import build_authorize_url, exchange_code_for_tokens
from bhiksha.integrations.schwab.settings import SchwabSettings


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: python -m bhiksha.tools.schwab_auth [url|exchange <returned_url>]")
        return 1

    settings = SchwabSettings.from_env()
    command = argv[0]

    if command == "url":
        print(build_authorize_url(settings))
        return 0

    if command == "exchange":
        if len(argv) < 2:
            print("Usage: python -m bhiksha.tools.schwab_auth exchange '<returned_url>'")
            return 1
        returned_url = argv[1]

        async def runner() -> None:
            await exchange_code_for_tokens(returned_url, settings)

        asyncio.run(runner())
        print(f"Schwab tokens stored in {settings.token_file}")
        return 0

    print(f"Unknown command: {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

