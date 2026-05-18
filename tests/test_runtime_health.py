import asyncio

from bhiksha.app.bootstrap import build_runtime


def test_runtime_health_report_contains_configured_providers() -> None:
    runtime = build_runtime()
    report = asyncio.run(runtime.health_report())
    names = {item.name for item in report.provider_health}
    assert names == {"public", "schwab", "schwab_token"}
