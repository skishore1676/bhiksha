"""Read-only market-data seam for the research observer.

The protocol intentionally exposes completed bars and quotes only.  The local
historical implementation reads a separate replay artifact; it has no broker,
order, active-plan, reconciliation, or Sheet capability.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol


class ResearchMarketData(Protocol):
    def completed_bars(self, candidate_id: str, symbol: str) -> Sequence[Mapping[str, Any]]:
        """Return completed historical/current bars for one candidate."""

    def quotes(self, candidate_id: str, symbol: str) -> Sequence[Mapping[str, Any]]:
        """Return read-only quotes for one candidate."""


class HistoricalResearchMarketData:
    """Separate local replay data for the same prospective request shape."""

    def __init__(self, candidates: Mapping[str, Mapping[str, Any]]) -> None:
        self._candidates = {str(key): dict(value) for key, value in candidates.items()}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HistoricalResearchMarketData:
        if raw.get("schema") != "research.historical_market_data.v1":
            raise ValueError("invalid historical research market-data schema")
        if raw.get("mode") != "historical_replay":
            raise ValueError("historical market data must declare historical_replay mode")
        candidates = raw.get("candidates")
        if not isinstance(candidates, Mapping) or not candidates:
            raise ValueError("historical market data requires candidate mappings")
        normalized: dict[str, Mapping[str, Any]] = {}
        for candidate_id, value in candidates.items():
            if not isinstance(candidate_id, str) or not isinstance(value, Mapping):
                raise ValueError("historical candidate data must be named objects")
            bars = value.get("bars")
            quotes = value.get("quotes")
            if not isinstance(bars, list) or not isinstance(quotes, list):
                raise ValueError(f"historical data must declare bars and quotes: {candidate_id}")
            normalized[candidate_id] = {
                "bars": list(bars),
                "quotes": list(quotes),
            }
        return cls(normalized)

    @classmethod
    def from_path(cls, path: str | Path) -> HistoricalResearchMarketData:
        source = Path(path).expanduser().resolve()
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("historical market data must contain one JSON object")
        return cls.from_mapping(payload)

    def completed_bars(self, candidate_id: str, symbol: str) -> Sequence[Mapping[str, Any]]:
        del symbol
        value = self._candidates.get(candidate_id)
        return list(value.get("bars", [])) if value else []

    def quotes(self, candidate_id: str, symbol: str) -> Sequence[Mapping[str, Any]]:
        del symbol
        value = self._candidates.get(candidate_id)
        return list(value.get("quotes", [])) if value else []


__all__ = ["HistoricalResearchMarketData", "ResearchMarketData"]
