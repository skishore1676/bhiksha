"""Strategy registry for runtime plugins."""

from __future__ import annotations

from dataclasses import dataclass, field

from bhiksha.strategy.base import StrategyPlugin
from bhiksha.strategy.market_impulse import MarketImpulseStrategy


@dataclass(slots=True)
class StrategyRegistry:
    _strategies: dict[str, StrategyPlugin] = field(default_factory=dict)

    def register(self, strategy: StrategyPlugin) -> None:
        self._strategies[strategy.key] = strategy

    def get(self, key: str) -> StrategyPlugin:
        try:
            return self._strategies[key]
        except KeyError as exc:
            raise KeyError(f"Unknown strategy key: {key}") from exc


def default_strategy_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(MarketImpulseStrategy())
    return registry

