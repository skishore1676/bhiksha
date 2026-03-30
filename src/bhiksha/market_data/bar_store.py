"""In-memory rolling bar storage."""

from __future__ import annotations

from collections import defaultdict, deque

from bhiksha.domain.models import Bar


class RollingBarStore:
    """Simple symbol-keyed rolling bar store for the live runtime."""

    def __init__(self, max_bars_per_symbol: int = 1000) -> None:
        self._bars: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=max_bars_per_symbol))

    def append(self, bar: Bar) -> None:
        self._bars[bar.symbol].append(bar)

    def extend(self, symbol: str, bars: list[Bar]) -> None:
        for bar in bars:
            if bar.symbol != symbol:
                raise ValueError(f"Expected bars for {symbol}, found {bar.symbol}")
            self._bars[symbol].append(bar)

    def get(self, symbol: str) -> list[Bar]:
        return list(self._bars[symbol])

    def latest(self, symbol: str) -> Bar | None:
        values = self._bars[symbol]
        return values[-1] if values else None
