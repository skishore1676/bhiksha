"""Public option-chain access and normalization."""

from __future__ import annotations

from datetime import date
from typing import Any

from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.execution.brokers.public.account import get_primary_account_id
from bhiksha.execution.brokers.public.client import PublicApiClient
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings


class PublicOptionChainService:
    """Use Public market data for option contract discovery."""

    def __init__(
        self,
        client: PublicApiClient | None = None,
        settings: PublicBrokerSettings | None = None,
        *,
        instrument_type: str = "EQUITY",
    ) -> None:
        self.settings = settings or PublicBrokerSettings.from_env()
        self.client = client or PublicApiClient(self.settings)
        self.instrument_type = instrument_type

    async def close(self) -> None:
        await self.client.close()

    async def get_chain(
        self,
        symbol: str,
        *,
        contract_type: str = "ALL",
        from_date: date | None = None,
        to_date: date | None = None,
        strike_count: int = 20,
    ) -> list[OptionContractSnapshot]:
        del strike_count  # Public returns the available chain; filtering happens locally.
        account_id = await get_primary_account_id(client=self.client, settings=self.settings)
        expirations_payload = await self.client.post(
            f"/userapigateway/marketdata/{account_id}/option-expirations",
            json_data={"instrument": {"symbol": symbol, "type": self.instrument_type}},
        )
        expirations = _filter_expirations(
            expirations_payload.get("expirations") or [],
            from_date=from_date,
            to_date=to_date,
        )
        contracts: list[OptionContractSnapshot] = []
        for expiration in expirations:
            payload = await self.client.post(
                f"/userapigateway/marketdata/{account_id}/option-chain",
                json_data={
                    "instrument": {"symbol": symbol, "type": self.instrument_type},
                    "expirationDate": expiration,
                },
            )
            contracts.extend(
                self._parse_chain(
                    symbol,
                    payload,
                    as_of=from_date or date.today(),
                    contract_type=contract_type,
                )
            )
        return contracts

    @staticmethod
    def _parse_chain(
        symbol: str,
        payload: dict[str, Any],
        *,
        as_of: date | None = None,
        contract_type: str = "ALL",
    ) -> list[OptionContractSnapshot]:
        contracts: list[OptionContractSnapshot] = []
        normalized_type = contract_type.upper()
        as_of = as_of or date.today()
        for payload_key, side in (("calls", "CALL"), ("puts", "PUT")):
            if normalized_type not in {"ALL", side}:
                continue
            for entry in payload.get(payload_key) or []:
                details = entry.get("optionDetails") or {}
                instrument = entry.get("instrument") or {}
                expiration = str(details.get("expirationDate") or payload.get("expirationDate") or "")
                if not expiration:
                    expiration = _expiration_from_symbol(str(instrument.get("symbol") or ""))
                expiration_date = _parse_date(expiration)
                dte = max((expiration_date - as_of).days, 0) if expiration_date is not None else 0
                contracts.append(
                    OptionContractSnapshot(
                        option_symbol=str(instrument.get("symbol") or "").replace(" ", ""),
                        underlying_symbol=str(payload.get("baseSymbol") or symbol),
                        contract_type=side,
                        expiration_date=expiration,
                        dte=dte,
                        strike=_maybe_float(details.get("strikePrice")) or 0.0,
                        delta=_maybe_float((details.get("greeks") or {}).get("delta")),
                        bid=_maybe_float(entry.get("bid")),
                        ask=_maybe_float(entry.get("ask")),
                        open_interest=_maybe_int(entry.get("openInterest")),
                    )
                )
        return contracts


def _filter_expirations(
    expirations: list[str],
    *,
    from_date: date | None,
    to_date: date | None,
) -> list[str]:
    selected: list[str] = []
    for raw in expirations:
        parsed = _parse_date(raw)
        if parsed is None:
            continue
        if from_date is not None and parsed < from_date:
            continue
        if to_date is not None and parsed > to_date:
            continue
        selected.append(raw)
    return selected


def _expiration_from_symbol(symbol: str) -> str:
    # OCC compact symbols usually include YYMMDD after the root.
    digits = "".join(ch for ch in symbol if ch.isdigit())
    if len(digits) < 6:
        return ""
    yymmdd = digits[:6]
    return f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
