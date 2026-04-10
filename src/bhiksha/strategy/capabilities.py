"""Shared strategy capability metadata."""

from __future__ import annotations


NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS = frozenset({"manual_breakout", "market_impulse"})


def supports_native_algorithmic_exit(strategy_key: str | None) -> bool:
    """Return whether the strategy has a dedicated runtime-managed exit implementation."""
    normalized = (strategy_key or "").strip().lower()
    return normalized in NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS
