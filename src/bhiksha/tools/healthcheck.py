"""Dry-run startup health command."""

from __future__ import annotations

import asyncio

from bhiksha.app.bootstrap import build_runtime


async def _run() -> None:
    runtime = build_runtime()
    report = await runtime.health_report()
    print(f"DRY_RUN={report.dry_run}")
    print(f"DEPLOYMENTS={','.join(report.enabled_deployments)}")
    for item in report.provider_health:
        print(f"{item.name.upper()}={item.ok}:{item.detail}")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

