"""Schwab option-chain access and normalization."""

from __future__ import annotations

from datetime import date, datetime

from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.integrations.schwab.client import SchwabApiClient


class SchwabOptionChainService:
    """Use Schwab option chains for contract discovery."""

    def __init__(self, client: SchwabApiClient | None = None) -> None:
        self.client = client or SchwabApiClient()

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
        payload = await self.client.option_chain(
            symbol,
            contract_type=contract_type,
            strike_count=strike_count,
            from_date=from_date,
            to_date=to_date,
        )
        return self._parse_chain(symbol, payload)

    @staticmethod
    def _parse_chain(symbol: str, payload: dict) -> list[OptionContractSnapshot]:
        contracts: list[OptionContractSnapshot] = []
        for side in ("callExpDateMap", "putExpDateMap"):
            expiration_map = payload.get(side, {}) or {}
            for expiry_key, strikes in expiration_map.items():
                expiry_date = expiry_key.split(":")[0]
                for strike_key, entries in strikes.items():
                    for entry in entries:
                        contracts.append(
                            OptionContractSnapshot(
                                option_symbol=str(entry.get("symbol", "")).replace(" ", ""),
                                underlying_symbol=symbol,
                                contract_type=str(entry.get("putCall", "")).upper(),
                                expiration_date=expiry_date,
                                dte=int(entry.get("daysToExpiration", 0)),
                                strike=float(entry.get("strikePrice", float(strike_key))),
                                delta=_maybe_float(entry.get("delta")),
                                bid=_maybe_float(entry.get("bid")),
                                ask=_maybe_float(entry.get("ask")),
                                open_interest=_maybe_int(entry.get("openInterest")),
                            )
                        )
        return contracts


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

