"""Read-only option snapshot seams for chart-scenario shadowing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import OptionQuoteSnapshot, as_utc


@runtime_checkable
class ReadOnlyOptionSnapshotSource(Protocol):
    """The only quote capability the observer consumes."""

    def get_snapshot(
        self, *, scenario: Any, at: datetime
    ) -> OptionQuoteSnapshot | None:
        """Return a persisted/read-only snapshot, or ``None`` if unavailable."""


_ORDER_METHODS = frozenset(
    {
        "place_order",
        "submit_order",
        "cancel_order",
        "replace_order",
        "reconcile",
        "square_off",
        "close_position",
        "create_order",
    }
)


def ensure_read_only_quote_source(source: Any) -> ReadOnlyOptionSnapshotSource:
    """Accept only the package's sealed, data-only snapshot containers.

    An arbitrary callback can hide effects inside ``get_snapshot`` even when it
    exposes no order-named method.  Exact-type acceptance keeps executable
    adapters outside the observer process.
    """

    if source is None:
        return None  # type: ignore[return-value]
    exposed = {name for name in _ORDER_METHODS if callable(getattr(source, name, None))}
    if exposed:
        raise TypeError(
            "option snapshot source exposes prohibited order capability: "
            + ", ".join(sorted(exposed))
        )
    if type(source) not in {StaticOptionSnapshotSource, PersistedOptionSnapshotSource}:
        raise TypeError(
            "option snapshot source must be a sealed data-only StaticOptionSnapshotSource "
            "or PersistedOptionSnapshotSource"
        )
    getter = getattr(source, "get_snapshot", None)
    if not callable(getter):
        raise TypeError("option snapshot source must implement get_snapshot")
    return source


class StaticOptionSnapshotSource:
    """A deterministic in-memory source for a caller-provided snapshot set."""

    def __init__(
        self,
        snapshots: Mapping[str, OptionQuoteSnapshot | Mapping[str, Any]]
        | Sequence[OptionQuoteSnapshot | Mapping[str, Any]],
    ) -> None:
        values: list[OptionQuoteSnapshot] = []
        if isinstance(snapshots, Mapping):
            iterable = snapshots.values()
        else:
            iterable = snapshots
        for value in iterable:
            values.append(
                value
                if isinstance(value, OptionQuoteSnapshot)
                else OptionQuoteSnapshot.from_mapping(value)
            )
        self._snapshots = tuple(values)

    def get_snapshot(
        self, *, scenario: Any, at: datetime
    ) -> OptionQuoteSnapshot | None:
        scenario_id = str(getattr(scenario, "scenario_id", ""))
        symbol = str(getattr(scenario, "symbol", ""))
        candidates = [
            quote
            for quote in self._snapshots
            if quote.quote_time <= as_utc(at)
            and (
                quote.scenario_id == scenario_id
                or (quote.scenario_id is None and quote.underlying_symbol == symbol)
            )
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda quote: (quote.is_selected, quote.quote_time, quote.snapshot_id)
        )
        return candidates[-1]


class PersistedOptionSnapshotSource(StaticOptionSnapshotSource):
    """Read snapshots from an immutable JSON export without network access."""

    @classmethod
    def from_json(cls, path: str | Path) -> PersistedOptionSnapshotSource:
        import json

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            raw = payload.get(
                "snapshots", payload.get("quotes", payload.get("option_snapshots"))
            )
        else:
            raw = payload
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise TypeError("quote snapshot file must contain a snapshots array")
        return cls(raw)


def quote_source_from_payload(payload: Any) -> PersistedOptionSnapshotSource:
    """Build the same read-only adapter from a fixture object."""

    if isinstance(payload, Mapping):
        raw = payload.get(
            "snapshots", payload.get("quotes", payload.get("option_snapshots"))
        )
    else:
        raw = payload
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise TypeError("quote fixture must contain a snapshots array")
    return PersistedOptionSnapshotSource(raw)


__all__ = [
    "PersistedOptionSnapshotSource",
    "ReadOnlyOptionSnapshotSource",
    "StaticOptionSnapshotSource",
    "ensure_read_only_quote_source",
    "quote_source_from_payload",
]
