"""Order payload builders and execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import uuid

from bhiksha.execution.brokers.public.adapter import PublicBrokerAdapter


def round_price(value: float) -> float:
    """Round to standard 2-decimal option pricing."""
    return round(float(value) + 1e-9, 2)


def normalize_option_symbol(symbol: str) -> str:
    """Normalize an OCC symbol by stripping Public-style suffixes."""
    symbol = symbol.strip().upper().replace(" ", "")
    if symbol.endswith("-OPTION"):
        symbol = symbol[:-7]
    return symbol


@dataclass(slots=True)
class OrderResult:
    order_id: str | None
    error: str | None = None


@dataclass(slots=True)
class PublicQuote:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    open_interest: int | None = None
    outcome: str | None = None

    @property
    def spread_pct(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return (self.ask - self.bid) / mid

    @property
    def entry_reference_price(self) -> float | None:
        return self.ask or self.last or self.bid


@dataclass(slots=True)
class PreflightCheck:
    payload: dict
    current_increment: float | None = None
    buying_power_requirement: float | None = None
    estimated_cost: float | None = None


def snap_price(value: float, increment: float, *, side: str) -> float:
    """Snap a price to the broker-supported increment."""
    scaled = value / increment
    snapped = math.ceil(scaled - 1e-9) if side.upper() == "BUY" else math.floor(scaled + 1e-9)
    return round_price(snapped * increment)


class OrderManager:
    """Minimal Public order manager for Day 1 single-leg options."""

    def __init__(self, broker: PublicBrokerAdapter | None = None) -> None:
        self.broker = broker or PublicBrokerAdapter()

    async def close(self) -> None:
        await self.broker.close()

    async def get_option_quote(self, option_symbol: str) -> PublicQuote:
        payload = await self.broker.get_quotes(
            [{"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"}]
        )
        quotes = payload.get("quotes", []) or []
        if not quotes:
            raise ValueError(f"No Public quote returned for {option_symbol}")
        quote = quotes[0]
        return PublicQuote(
            symbol=normalize_option_symbol(quote.get("instrument", {}).get("symbol", option_symbol)),
            bid=_maybe_float(quote.get("bid")),
            ask=_maybe_float(quote.get("ask")),
            last=_maybe_float(quote.get("last")),
            open_interest=_maybe_int(quote.get("openInterest")),
            outcome=quote.get("outcome"),
        )

    async def preflight_entry(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
    ) -> PreflightCheck:
        payload = self._entry_payload(option_symbol, limit_price, quantity)
        response = await self.broker.preflight_single_leg(payload)
        increment = _maybe_float((response.get("priceIncrement") or {}).get("currentIncrement"))
        if increment:
            payload["limitPrice"] = f"{snap_price(limit_price, increment, side='BUY'):.2f}"
        return PreflightCheck(
            payload=payload,
            current_increment=increment,
            buying_power_requirement=_maybe_float(response.get("buyingPowerRequirement")),
            estimated_cost=_maybe_float(response.get("estimatedCost")),
        )

    async def place_entry_order(self, option_symbol: str, limit_price: float, quantity: int) -> OrderResult:
        return await self._submit(self._entry_payload(option_symbol, limit_price, quantity))

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int) -> OrderResult:
        return await self._submit(
            {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "STOP",
                "expiration": {"timeInForce": "DAY"},
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
                "stopPrice": f"{round_price(stop_price):.2f}",
            }
        )

    async def place_target_order(self, option_symbol: str, limit_price: float, quantity: int) -> OrderResult:
        return await self._submit(
            {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "LIMIT",
                "expiration": {"timeInForce": "DAY"},
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
                "limitPrice": f"{round_price(limit_price):.2f}",
            }
        )

    async def place_square_off_order(self, option_symbol: str, quantity: int) -> OrderResult:
        return await self._submit(
            {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "MARKET",
                "expiration": {"timeInForce": "DAY"},
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
            }
        )

    async def get_order_status(self, order_id: str) -> tuple[str | None, dict | None, str | None]:
        try:
            payload = await self.broker.get_order(order_id)
            return payload.get("status"), payload, None
        except Exception as exc:
            return None, None, str(exc)

    async def cancel_order(self, order_id: str) -> tuple[bool, str | None]:
        try:
            await self.broker.cancel_order(order_id)
            return True, None
        except Exception as exc:
            return False, str(exc)

    async def wait_for_fill(
        self,
        order_id: str,
        *,
        timeout_seconds: int = 20,
        poll_seconds: int = 2,
    ) -> tuple[bool, dict | None, str | None]:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        last_payload = None
        while datetime.now(timezone.utc) < deadline:
            status, payload, error = await self.get_order_status(order_id)
            last_payload = payload
            if error:
                return False, payload, error
            normalized = (status or "").upper()
            if normalized == "FILLED":
                return True, payload, None
            if normalized in {"REJECTED", "CANCELED", "EXPIRED"}:
                return False, payload, normalized
            await __import__("asyncio").sleep(poll_seconds)
        return False, last_payload, "fill_timeout"

    async def _submit(self, payload: dict[str, str]) -> OrderResult:
        try:
            response = await self.broker.place_order(payload)
            order_id = response.get("orderId") or response.get("id")
            return OrderResult(order_id=str(order_id) if order_id else None, error=None if order_id else "missing_order_id")
        except Exception as exc:
            return OrderResult(order_id=None, error=str(exc))

    @staticmethod
    def _entry_payload(option_symbol: str, limit_price: float, quantity: int) -> dict[str, str | dict[str, str]]:
        return {
            "orderId": str(uuid.uuid4()),
            "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
            "orderSide": "BUY",
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": str(int(quantity)),
            "openCloseIndicator": "OPEN",
            "limitPrice": f"{round_price(limit_price):.2f}",
        }


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
