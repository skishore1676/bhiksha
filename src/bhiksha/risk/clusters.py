"""Confirmed, app-owned correlation clusters used by live risk controls."""

from __future__ import annotations


# Deterministic and intentionally small. An absent symbol is unmapped rather
# than guessed; extend only when the relationship is an operator-confirmed
# portfolio-risk fact.
CLUSTER_BY_UNDERLYING: dict[str, str] = {
    "SPY": "broad_market",
    "QQQ": "broad_market",
    "IWM": "broad_market",
    "DIA": "broad_market",
    "VOO": "broad_market",
    "VTI": "broad_market",
    "NVDA": "semiconductors",
    "AMD": "semiconductors",
    "SMH": "semiconductors",
    "MU": "semiconductors",
    "INTC": "semiconductors",
    "TSM": "semiconductors",
    "AVGO": "semiconductors",
    "SOXL": "semiconductors",
    "SOXX": "semiconductors",
    "VIX": "volatility",
    "VXX": "volatility",
    "UVXY": "volatility",
    "SVXY": "volatility",
    "VIXY": "volatility",
}


def correlation_cluster(symbol: str | None) -> str | None:
    if not symbol:
        return None
    return CLUSTER_BY_UNDERLYING.get(str(symbol).strip().upper())
