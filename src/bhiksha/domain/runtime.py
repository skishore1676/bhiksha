"""Runtime-level summaries."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class ProviderHealth:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True, frozen=True)
class StartupReport:
    dry_run: bool
    enabled_deployments: list[str]
    provider_health: list[ProviderHealth] = field(default_factory=list)

