"""Exact, replayable normalization of one selected Schwab option quote."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.domain.models import OptionContractSnapshot

from .models import OptionQuoteSnapshot, timestamp_json

_FORBIDDEN_RAW_KEYS = (
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "cookie",
    "account",
    "order",
)


def normalize_option_symbol(value: str) -> str:
    return "".join(str(value).split()).upper()


def selected_raw_quote(
    payload: Mapping[str, Any], *, option_symbol: str
) -> dict[str, Any]:
    """Seal one selected provider response with its authenticated OCC key."""

    normalized = normalize_option_symbol(option_symbol)
    matches = [
        (key, value)
        for key, value in payload.items()
        if normalize_option_symbol(str(key)) == normalized
        and isinstance(value, Mapping)
    ]
    if len(matches) != 1:
        raise ValueError("Schwab quote response omitted the exact selected contract")
    _key, value = matches[0]
    inner = json.loads(json.dumps(dict(value), allow_nan=False))
    envelope = {normalized: inner}
    _reject_sensitive_keys(envelope)
    return envelope


def build_live_quote(
    raw_source: Mapping[str, Any],
    *,
    option_symbol: str,
    selected_contract: OptionContractSnapshot,
    acquired_at: datetime,
    policy_hash: str,
    selection_mode: str,
) -> dict[str, Any]:
    """Normalize a raw provider quote and prove its exact selected-contract join."""

    raw = json.loads(json.dumps(dict(raw_source), allow_nan=False))
    _reject_sensitive_keys(raw)
    normalized_symbol = normalize_option_symbol(option_symbol)
    selected_symbol = normalize_option_symbol(selected_contract.option_symbol)
    if normalized_symbol != selected_symbol:
        raise ValueError("selected quote symbol differs from chain snapshot")
    if set(raw) != {normalized_symbol} or not isinstance(
        raw.get(normalized_symbol), Mapping
    ):
        raise ValueError("raw quote response must retain the exact selected OCC key")
    selected_raw = raw[normalized_symbol]
    parsed_type, parsed_expiration, parsed_strike, parsed_root = occ_contract(
        normalized_symbol
    )
    selected_underlying = str(selected_contract.underlying_symbol).strip().upper()
    selected_type = str(selected_contract.contract_type).strip().upper()
    selected_expiration = date.fromisoformat(
        str(selected_contract.expiration_date)
    ).isoformat()
    if (
        parsed_root != selected_underlying
        or parsed_type != selected_type
        or parsed_expiration != selected_expiration
        or not decimal_equal(parsed_strike, selected_contract.strike)
    ):
        raise ValueError("selected chain snapshot conflicts with parsed OCC identity")

    quote = (
        selected_raw.get("quote")
        if isinstance(selected_raw.get("quote"), Mapping)
        else selected_raw
    )
    reference = (
        selected_raw.get("reference")
        if isinstance(selected_raw.get("reference"), Mapping)
        else {}
    )
    provider_underlying = (
        str(reference["underlyingSymbol"]).strip().upper()
        if "underlyingSymbol" in reference
        else selected_underlying
    )
    provider_type = (
        str(reference["contractType"]).strip().upper()
        if "contractType" in reference
        else selected_type
    )
    provider_expiration = (
        date.fromisoformat(str(reference["expirationDate"])[:10]).isoformat()
        if "expirationDate" in reference
        else selected_expiration
    )
    provider_strike = (
        optional_float(reference["strikePrice"])
        if "strikePrice" in reference
        else float(selected_contract.strike)
    )
    if (
        provider_underlying != selected_underlying
        or provider_type != selected_type
        or provider_expiration != selected_expiration
        or not decimal_equal(provider_strike, selected_contract.strike)
    ):
        raise ValueError(
            "provider quote static identity differs from selected contract"
        )

    bid = optional_float(quote.get("bidPrice", quote.get("bid")))
    ask = optional_float(quote.get("askPrice", quote.get("ask")))
    last = optional_float(quote.get("lastPrice", quote.get("last")))
    quote_time = provider_timestamp(
        quote.get("quoteTime") if bid is not None and ask is not None else None,
        quote.get("tradeTime"),
    )
    if quote_time is None:
        raise ValueError("Schwab option quote has no provider timestamp")
    delta = optional_float(quote.get("delta"))
    open_interest = optional_int(quote.get("openInterest"))

    raw_source_hash = canonical_sha256(raw)
    facts: dict[str, Any] = {
        "option_symbol": normalized_symbol,
        "underlying_symbol": selected_underlying,
        "contract_type": selected_type,
        "expiration_date": selected_expiration,
        "quote_time": timestamp_json(quote_time),
        "source_id": "schwab-option-quote",
        "bid": bid,
        "ask": ask,
        "last": last,
        "strike": float(selected_contract.strike),
        "delta": delta,
        "open_interest": open_interest,
        "scenario_id": None,
        "is_selected": True,
        "provenance": {
            "provider_id": "schwab",
            "option_selection_policy_hash": policy_hash,
            "selection_mode": selection_mode,
        },
        "acquired_at": timestamp_json(acquired_at),
        "raw_source": raw,
        "raw_source_hash": raw_source_hash,
    }
    identity = canonical_sha256(
        {"schema": "bhiksha.chart-scenario-live-option-snapshot.v2", **facts}
    )
    snapshot = {
        "snapshot_id": "schwab-" + identity[:24],
        **facts,
        "snapshot_hash": None,
    }
    return OptionQuoteSnapshot.from_mapping(snapshot).to_dict()


def occ_contract(option_symbol: str) -> tuple[str, str, float, str]:
    compact = normalize_option_symbol(option_symbol)
    index = next((i for i, char in enumerate(compact) if char.isdigit()), -1)
    if index < 1 or len(compact) != index + 15:
        raise ValueError("selected option symbol is not an exact OCC symbol")
    root = compact[:index]
    yymmdd = compact[index : index + 6]
    side = compact[index + 6]
    strike_digits = compact[index + 7 : index + 15]
    if side not in {"C", "P"} or not (yymmdd + strike_digits).isdigit():
        raise ValueError("selected option symbol is not an exact OCC symbol")
    expiration = date(
        2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    ).isoformat()
    return (
        "CALL" if side == "C" else "PUT",
        expiration,
        int(strike_digits) / 1000,
        root,
    )


def provider_timestamp(*values: Any) -> datetime | None:
    for value in values:
        if value is None:
            continue
        try:
            if isinstance(value, int | float) and not isinstance(value, bool):
                return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            continue
    return None


def decimal_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return Decimal(str(left)).normalize() == Decimal(str(right)).normalize()
    except (InvalidOperation, TypeError, ValueError):
        return False


def optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if any(marker in normalized for marker in _FORBIDDEN_RAW_KEYS):
                raise ValueError(
                    "raw quote response contains a forbidden sensitive key"
                )
            _reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


__all__ = [
    "build_live_quote",
    "decimal_equal",
    "normalize_option_symbol",
    "occ_contract",
    "provider_timestamp",
    "selected_raw_quote",
]
