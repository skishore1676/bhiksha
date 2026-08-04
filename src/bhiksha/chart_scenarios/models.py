"""Small, broker-free value objects used by the chart-scenario observer.

The shared kernel owns the scenario packet itself.  This module owns only the
runtime observations supplied to the observer: completed underlying bars and
read-only option quote snapshots.  Neither value object has an order-shaped
method or a reference to Bhiksha's execution stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Any, Mapping

from mala_bhiksha_kernel import canonical_sha256


def as_utc(value: datetime | str) -> datetime:
    """Parse an aware timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp must be an RFC 3339 string or aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def timestamp_json(value: datetime) -> str:
    """Return the one timestamp spelling used in observer receipts."""

    return as_utc(value).isoformat().replace("+00:00", "Z")


def finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


@dataclass(frozen=True, slots=True)
class CompletedBar:
    """One completed underlying bar supplied by a caller or replay fixture."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    completed: bool = True
    bar_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, finite_float(getattr(self, field), field))
        if self.volume is not None:
            object.__setattr__(self, "volume", finite_float(self.volume, "volume"))
        if not self.completed:
            raise ValueError("only completed bars may enter the chart-scenario observer")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar high/low do not contain open and close")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompletedBar":
        if not isinstance(value, Mapping):
            raise TypeError("bar observation must be an object")
        timestamp = value.get("timestamp", value.get("bar_time", value.get("at")))
        if timestamp is None:
            raise ValueError("bar observation requires timestamp")
        required = ("open", "high", "low", "close")
        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(f"bar observation missing {', '.join(missing)}")
        return cls(
            timestamp=as_utc(timestamp),
            open=value["open"],
            high=value["high"],
            low=value["low"],
            close=value["close"],
            volume=value.get("volume"),
            completed=bool(value.get("completed", True)),
            bar_id=str(value["bar_id"]) if value.get("bar_id") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": timestamp_json(self.timestamp),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "completed": True,
            **({"bar_id": self.bar_id} if self.bar_id else {}),
        }


@dataclass(frozen=True, slots=True)
class OptionQuoteSnapshot:
    """An immutable, provenance-bearing option quote snapshot.

    A snapshot is a mark observation, never a fill.  ``snapshot_hash`` is the
    identity of the supplied quote facts and deliberately does not include a
    synthetic entry price or any observer state.
    """

    snapshot_id: str
    option_symbol: str
    underlying_symbol: str
    contract_type: str
    expiration_date: str
    quote_time: datetime
    source_id: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    strike: float | None = None
    delta: float | None = None
    open_interest: int | None = None
    scenario_id: str | None = None
    is_selected: bool = False
    provenance: Mapping[str, Any] | None = None
    snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "snapshot_id",
            "option_symbol",
            "underlying_symbol",
            "contract_type",
            "expiration_date",
            "source_id",
        ):
            value = str(getattr(self, field)).strip()
            if not value:
                raise ValueError(f"{field} must be non-empty")
            object.__setattr__(self, field, value)
        object.__setattr__(self, "quote_time", as_utc(self.quote_time))
        for field in ("bid", "ask", "last", "strike", "delta"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, finite_float(value, field))
        if self.open_interest is not None:
            if isinstance(self.open_interest, bool) or int(self.open_interest) != self.open_interest:
                raise TypeError("open_interest must be an integer")
            if self.open_interest < 0:
                raise ValueError("open_interest must be non-negative")
            object.__setattr__(self, "open_interest", int(self.open_interest))
        computed = canonical_sha256(self._hash_payload())
        if self.snapshot_hash is not None:
            supplied = str(self.snapshot_hash).removeprefix("sha256:")
            if supplied != computed:
                raise ValueError("snapshot_hash does not match quote facts")
        object.__setattr__(self, "snapshot_hash", computed)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OptionQuoteSnapshot":
        if not isinstance(value, Mapping):
            raise TypeError("option quote must be an object")
        quote_time = value.get("quote_time", value.get("timestamp", value.get("at")))
        if quote_time is None:
            raise ValueError("option quote requires quote_time")
        option_symbol = value.get("option_symbol", value.get("contract_symbol"))
        underlying = value.get("underlying_symbol", value.get("symbol"))
        if not option_symbol or not underlying:
            raise ValueError("option quote requires option_symbol and underlying_symbol")
        return cls(
            snapshot_id=str(value.get("snapshot_id", value.get("quote_id", "quote"))),
            option_symbol=str(option_symbol),
            underlying_symbol=str(underlying),
            contract_type=str(value.get("contract_type", value.get("option_type", "unknown"))),
            expiration_date=str(value.get("expiration_date", value.get("expiration", "unknown"))),
            quote_time=as_utc(quote_time),
            source_id=str(value.get("source_id", value.get("source", "caller_snapshot"))),
            bid=value.get("bid"),
            ask=value.get("ask"),
            last=value.get("last", value.get("mark")),
            strike=value.get("strike"),
            delta=value.get("delta"),
            open_interest=value.get("open_interest"),
            scenario_id=str(value["scenario_id"]) if value.get("scenario_id") else None,
            is_selected=bool(value.get("is_selected", value.get("selected", False))),
            provenance=value.get("provenance"),
            snapshot_hash=value.get("snapshot_hash"),
        )

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "option_symbol": self.option_symbol,
            "underlying_symbol": self.underlying_symbol,
            "contract_type": self.contract_type,
            "expiration_date": self.expiration_date,
            "quote_time": timestamp_json(self.quote_time),
            "source_id": self.source_id,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "strike": self.strike,
            "delta": self.delta,
            "open_interest": self.open_interest,
            "scenario_id": self.scenario_id,
            "is_selected": self.is_selected,
            "provenance": dict(self.provenance or {}),
        }

    @property
    def mark(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def eligible(self) -> bool:
        mark = self.mark
        if mark is None or not math.isfinite(mark) or mark <= 0:
            return False
        if self.bid is not None and self.bid < 0:
            return False
        if self.ask is not None and self.ask < 0:
            return False
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._hash_payload()
        payload["snapshot_hash"] = self.snapshot_hash
        return payload

    def quote_provenance(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "source_id": self.source_id,
            "quote_time": timestamp_json(self.quote_time),
            "option_symbol": self.option_symbol,
            "underlying_symbol": self.underlying_symbol,
            "contract_type": self.contract_type,
            "expiration_date": self.expiration_date,
            "strike": self.strike,
            "delta": self.delta,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "provenance": dict(self.provenance or {}),
        }


__all__ = [
    "CompletedBar",
    "OptionQuoteSnapshot",
    "as_utc",
    "timestamp_json",
]
