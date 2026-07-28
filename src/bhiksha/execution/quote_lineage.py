"""Fail-closed timestamp lineage for Public bid/ask option quotes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


PUBLIC_QUOTE_TIMESTAMP_FIELD = "quoteTimestamp"
PUBLIC_BID_ASK_TIMESTAMP_FIELD = "bidTimestamp+askTimestamp"
PROVED_TWO_SIDED_QUOTE_TIMESTAMP_FIELDS = frozenset(
    {
        PUBLIC_QUOTE_TIMESTAMP_FIELD,
        PUBLIC_BID_ASK_TIMESTAMP_FIELD,
    }
)


@dataclass(frozen=True, slots=True)
class ProvedQuoteTimestampLineage:
    """Provider timestamps that prove the bid and ask belong to the snapshot."""

    quote_at: datetime
    field: str
    bid_at: datetime
    ask_at: datetime

    def ages_ms(self, observed_at: datetime) -> tuple[float, float]:
        observed = aware_utc(observed_at)
        return (
            (observed - self.bid_at).total_seconds() * 1_000,
            (observed - self.ask_at).total_seconds() * 1_000,
        )


def extract_public_quote_timestamp(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract only provider fields that prove bid/ask snapshot lineage.

    Public's explicit ``quoteTimestamp`` is the preferred two-sided timestamp.
    Otherwise both side-specific timestamps are required, and the older side is
    used as the conservative effective timestamp.  Last-trade and generic
    response timestamps intentionally do not qualify.
    """

    bid_timestamp = _string_or_none(payload.get("bidTimestamp"))
    ask_timestamp = _string_or_none(payload.get("askTimestamp"))

    quote_timestamp = payload.get(PUBLIC_QUOTE_TIMESTAMP_FIELD)
    if parse_provider_timestamp(quote_timestamp) is not None:
        return (
            str(quote_timestamp),
            PUBLIC_QUOTE_TIMESTAMP_FIELD,
            bid_timestamp,
            ask_timestamp,
        )

    bid_at = parse_provider_timestamp(bid_timestamp)
    ask_at = parse_provider_timestamp(ask_timestamp)
    if bid_at is None or ask_at is None:
        return None, None, bid_timestamp, ask_timestamp
    return (
        min(bid_at, ask_at).isoformat(),
        PUBLIC_BID_ASK_TIMESTAMP_FIELD,
        bid_timestamp,
        ask_timestamp,
    )


def proved_quote_timestamp_lineage(
    quote: Any,
    *,
    observed_at: datetime | None = None,
) -> ProvedQuoteTimestampLineage | None:
    """Validate a normalized quote's provider timestamp lineage.

    When an observation time is supplied, any provider timestamp in the future
    fails closed.  Freshness limits remain the caller's responsibility.
    """

    field = getattr(quote, "quote_timestamp_field", None)
    if field not in PROVED_TWO_SIDED_QUOTE_TIMESTAMP_FIELDS:
        return None

    if field == PUBLIC_QUOTE_TIMESTAMP_FIELD:
        quote_at = parse_provider_timestamp(
            getattr(quote, "quote_timestamp", None)
        )
        if quote_at is None:
            return None
        lineage = ProvedQuoteTimestampLineage(
            quote_at=quote_at,
            field=field,
            bid_at=quote_at,
            ask_at=quote_at,
        )
    else:
        bid_at = parse_provider_timestamp(getattr(quote, "bid_timestamp", None))
        ask_at = parse_provider_timestamp(getattr(quote, "ask_timestamp", None))
        quote_at = parse_provider_timestamp(
            getattr(quote, "quote_timestamp", None)
        )
        if bid_at is None or ask_at is None or quote_at is None:
            return None
        conservative = min(bid_at, ask_at)
        if quote_at != conservative:
            return None
        lineage = ProvedQuoteTimestampLineage(
            quote_at=conservative,
            field=field,
            bid_at=bid_at,
            ask_at=ask_at,
        )

    if observed_at is not None:
        bid_age_ms, ask_age_ms = lineage.ages_ms(observed_at)
        if bid_age_ms < 0 or ask_age_ms < 0:
            return None
    return lineage


def parse_provider_timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return aware_utc(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric /= 1_000
        try:
            return datetime.fromtimestamp(numeric, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return aware_utc(parsed)


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "PROVED_TWO_SIDED_QUOTE_TIMESTAMP_FIELDS",
    "PUBLIC_BID_ASK_TIMESTAMP_FIELD",
    "PUBLIC_QUOTE_TIMESTAMP_FIELD",
    "ProvedQuoteTimestampLineage",
    "extract_public_quote_timestamp",
    "parse_provider_timestamp",
    "proved_quote_timestamp_lineage",
]
