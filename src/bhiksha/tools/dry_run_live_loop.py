"""Continuous live-loop runtime."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from bhiksha.app.bootstrap import build_runtime


async def _run(max_bars: int | None, live: bool, session_payload: str | None) -> None:
    runtime = build_runtime(session_payload_path=session_payload)
    report = await runtime.health_report()
    for item in report.provider_health:
        print(f"HEALTH {item.name} ok={item.ok} detail={item.detail}")
    unhealthy = [item for item in report.provider_health if not item.ok]
    if unhealthy:
        names = ",".join(item.name for item in unhealthy)
        raise RuntimeError(f"Startup health check failed for: {names}")
    await runtime.run_session(live=live, max_bars=max_bars, output=print)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Bhiksha continuous live loop")
    parser.add_argument("--max-bars", type=int, default=None, help="Stop after this many newly closed bars")
    parser.add_argument("--live", action="store_true", help="Allow live order submission instead of dry-run planning")
    parser.add_argument(
        "--session-payload",
        type=str,
        default=None,
        help="Path to an active_session.json payload. When supplied, Bhiksha ignores config/deployments for this session.",
    )
    args = parser.parse_args(argv)
    asyncio.run(_run(args.max_bars, args.live, args.session_payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
