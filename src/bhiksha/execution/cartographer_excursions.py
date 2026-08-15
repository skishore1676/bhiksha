"""Strict entry-to-exit excursion facts for Cartographer shadow evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


def _result(*, coverage: str, reasons: list[str], mfe: float | None, mae: float | None) -> dict[str, Any]:
    return {
        "coverage": coverage,
        "coverage_reasons": reasons,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "decision_ready": coverage == "complete",
    }


def option_mfe_mae(
    *, trade_id: str, entry_at: datetime, exit_at: datetime, entry_price: float, marks: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Compute option MFE/MAE from complete, exact-trade marks only.

    Missing or partial coverage is explicit and never substituted with zero.
    """

    if entry_price <= 0 or exit_at < entry_at:
        raise ValueError("excursion interval and entry price must be valid")
    selected = [
        mark
        for mark in marks
        if mark.get("trade_id") == trade_id and entry_at <= mark.get("timestamp") <= exit_at
    ]
    if not selected:
        return _result(coverage="missing", reasons=["no_trade_keyed_marks"], mfe=None, mae=None)
    if any(mark.get("coverage") != "complete" for mark in selected):
        return _result(coverage="partial", reasons=["partial_option_mark"], mfe=None, mae=None)
    times = sorted(mark["timestamp"] for mark in selected)
    if times[-1] < exit_at:
        return _result(coverage="missing", reasons=["terminal_option_mark_missing"], mfe=None, mae=None)
    returns = [(float(mark["price"]) / entry_price - 1.0) for mark in selected]
    return _result(
        coverage="complete",
        reasons=[],
        mfe=round(max(returns), 8),
        mae=round(min(returns), 8),
    )


def underlying_mfe_mae(
    *, direction: str, entry_at: datetime, exit_at: datetime, entry_price: float, bars: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Compute post-entry underlying excursions without treating entry bars as separable."""

    if direction not in {"long", "short"} or entry_price <= 0 or exit_at < entry_at:
        raise ValueError("underlying excursion inputs are invalid")
    source = list(bars)
    if any(bar["start"] < entry_at < bar["end"] for bar in source):
        return _result(coverage="partial", reasons=["entry_bar_inseparable"], mfe=None, mae=None)
    selected = [bar for bar in source if entry_at <= bar["start"] and bar["end"] <= exit_at]
    if not selected:
        return _result(coverage="missing", reasons=["no_post_entry_underlying_bars"], mfe=None, mae=None)
    if any(bar.get("coverage") != "complete" for bar in selected):
        return _result(coverage="partial", reasons=["partial_underlying_bar"], mfe=None, mae=None)
    if max(bar["end"] for bar in selected) < exit_at:
        return _result(coverage="missing", reasons=["terminal_underlying_bar_missing"], mfe=None, mae=None)
    if direction == "long":
        favorable = [(float(bar["high"]) / entry_price - 1.0) for bar in selected]
        adverse = [(float(bar["low"]) / entry_price - 1.0) for bar in selected]
    else:
        favorable = [(entry_price / float(bar["low"]) - 1.0) for bar in selected]
        adverse = [(entry_price / float(bar["high"]) - 1.0) for bar in selected]
    return _result(
        coverage="complete",
        reasons=[],
        mfe=round(max(favorable), 8),
        mae=round(min(adverse), 8),
    )


__all__ = ["option_mfe_mae", "underlying_mfe_mae"]
