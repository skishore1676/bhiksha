"""Order payload builders and execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import uuid
from typing import Any

import httpx
from loguru import logger

from bhiksha.domain.enums import ExitMode
from bhiksha.execution.brokers.public.adapter import PublicBrokerAdapter

DEFAULT_OPTION_TICK_INCREMENT = 0.05


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
    quote_timestamp: str | None = None
    outcome: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round_price((self.bid + self.ask) / 2)

    @property
    def spread_abs(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round_price(self.ask - self.bid)

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

    @property
    def exit_reference_price(self) -> float | None:
        return self.bid or self.last or self.ask


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

    supports_concurrent_exit_orders = False
    allows_exit_submission_before_cancel_confirmation = True

    def __init__(self, broker: PublicBrokerAdapter | None = None) -> None:
        self.broker = broker or PublicBrokerAdapter()
        self._price_increments: dict[str, float] = {}
        self._underlying_price_increments: dict[str, float] = {}

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
            quote_timestamp=_quote_timestamp(quote),
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
            self._cache_price_increment(option_symbol, increment)
            payload["limitPrice"] = f"{snap_price(limit_price, increment, side='BUY'):.2f}"
        return PreflightCheck(
            payload=payload,
            current_increment=increment,
            buying_power_requirement=_maybe_float(response.get("buyingPowerRequirement")),
            estimated_cost=_maybe_float(response.get("estimatedCost")),
        )

    async def get_portfolio(self) -> dict[str, Any]:
        return await self.broker.get_portfolio()

    async def get_account_info(self) -> dict[str, Any]:
        return await self.broker.get_account_info()

    async def place_entry_order(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ) -> OrderResult:
        payload = self._entry_payload(option_symbol, limit_price, quantity, order_id=order_id)
        payload = await self._apply_increment_correction(payload, side="BUY", price_key="limitPrice")
        return await self._submit_with_increment_retry(payload, side="BUY", price_key="limitPrice")

    async def place_stop_loss_order(
        self,
        option_symbol: str,
        stop_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ) -> OrderResult:
        payload = {
            "orderId": order_id or str(uuid.uuid4()),
            "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
            "orderSide": "SELL",
            "orderType": "STOP",
            "expiration": {"timeInForce": "DAY"},
            "quantity": str(int(quantity)),
            "openCloseIndicator": "CLOSE",
            "stopPrice": f"{round_price(stop_price):.2f}",
        }
        payload = await self._apply_increment_correction(payload, side="SELL", price_key="stopPrice")
        return await self._submit_with_increment_retry(payload, side="SELL", price_key="stopPrice")

    async def place_target_order(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ) -> OrderResult:
        payload = {
            "orderId": order_id or str(uuid.uuid4()),
            "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
            "orderSide": "SELL",
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": str(int(quantity)),
            "openCloseIndicator": "CLOSE",
            "limitPrice": f"{round_price(limit_price):.2f}",
        }
        payload = await self._apply_increment_correction(payload, side="SELL", price_key="limitPrice")
        return await self._submit_with_increment_retry(payload, side="SELL", price_key="limitPrice")

    async def place_square_off_order(
        self,
        option_symbol: str,
        quantity: int,
        *,
        order_id: str | None = None,
    ) -> OrderResult:
        return await self.place_close_order(
            option_symbol,
            quantity,
            exit_mode=ExitMode.EMERGENCY,
            order_id=order_id,
        )

    async def place_close_order(
        self,
        option_symbol: str,
        quantity: int,
        *,
        exit_mode: ExitMode,
        limit_price: float | None = None,
        order_id: str | None = None,
    ) -> OrderResult:
        if exit_mode != ExitMode.EMERGENCY and limit_price is not None:
            payload = {
                "orderId": order_id or str(uuid.uuid4()),
                "instrument": {"symbol": normalize_option_symbol(option_symbol), "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "LIMIT",
                "expiration": {"timeInForce": "DAY"},
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
                "limitPrice": f"{round_price(limit_price):.2f}",
            }
            payload = await self._apply_increment_correction(payload, side="SELL", price_key="limitPrice")
            return await self._submit_with_increment_retry(payload, side="SELL", price_key="limitPrice")
        return await self._submit(
            {
                "orderId": order_id or str(uuid.uuid4()),
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

    async def _submit(self, payload: dict[str, Any]) -> OrderResult:
        try:
            response = await self.broker.place_order(payload)
            order_id = response.get("orderId") or response.get("id")
            return OrderResult(order_id=str(order_id) if order_id else None, error=None if order_id else "missing_order_id")
        except Exception as exc:
            return OrderResult(order_id=None, error=_exception_message(exc))

    async def _submit_with_increment_retry(
        self,
        payload: dict[str, Any],
        *,
        side: str,
        price_key: str,
    ) -> OrderResult:
        result = await self._submit(payload)
        if result.order_id or not result.error:
            return result
        increment = _extract_price_increment(result.error)
        if increment is None:
            return result
        symbol = normalize_option_symbol(str((payload.get("instrument") or {}).get("symbol", "")))
        if not symbol:
            return result
        self._cache_price_increment(symbol, increment)
        original_price = float(payload[price_key])
        corrected_price = snap_price(original_price, increment, side=side)
        payload[price_key] = f"{corrected_price:.2f}"
        logger.warning(
            "ORDER_RETRY_INCREMENT symbol={} side={} price_key={} increment={} original_price={} corrected_price={}",
            symbol,
            side,
            price_key,
            increment,
            f"{original_price:.2f}",
            payload[price_key],
        )
        return await self._submit(payload)

    async def _apply_increment_correction(
        self,
        payload: dict[str, Any],
        *,
        side: str,
        price_key: str,
    ) -> dict[str, Any]:
        if price_key not in payload:
            return payload
        symbol = normalize_option_symbol(str((payload.get("instrument") or {}).get("symbol", "")))
        increment = self._lookup_price_increment(symbol)
        if increment is None:
            try:
                response = await self.broker.preflight_single_leg(payload)
            except Exception:
                response = {}
            increment = _maybe_float((response.get("priceIncrement") or {}).get("currentIncrement"))
            if increment:
                self._cache_price_increment(symbol, increment)
        if increment:
            original_price = float(payload[price_key])
            corrected_price = snap_price(original_price, increment, side=side)
            payload[price_key] = f"{corrected_price:.2f}"
            if corrected_price != round_price(original_price):
                logger.debug(
                    "ORDER_PRICE_SNAPPED symbol={} side={} price_key={} increment={} original_price={} corrected_price={}",
                    symbol,
                    side,
                    price_key,
                    increment,
                    f"{original_price:.2f}",
                    payload[price_key],
                )
        return payload

    def _lookup_price_increment(self, option_symbol: str) -> float | None:
        symbol = normalize_option_symbol(option_symbol)
        increment = self._price_increments.get(symbol)
        if increment is not None:
            return increment
        underlying = underlying_symbol_from_option(symbol)
        if underlying is None:
            return None
        return self._underlying_price_increments.get(underlying)

    def _cache_price_increment(self, option_symbol: str, increment: float) -> None:
        symbol = normalize_option_symbol(option_symbol)
        self._price_increments[symbol] = increment
        underlying = underlying_symbol_from_option(symbol)
        if underlying is not None:
            self._underlying_price_increments[underlying] = increment
        logger.debug("PRICE_INCREMENT_LEARNED symbol={} underlying={} increment={}", symbol, underlying, increment)

    @staticmethod
    def _entry_payload(
        option_symbol: str,
        limit_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ) -> dict[str, str | dict[str, str]]:
        return {
            "orderId": order_id or str(uuid.uuid4()),
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


def _quote_timestamp(quote: dict[str, Any]) -> str | None:
    for key in ("timestamp", "quoteTimestamp", "lastTradeTime", "updatedAt", "asOf"):
        value = quote.get(key)
        if value is not None:
            return str(value)
    return None


_PRICE_INCREMENT_RE = re.compile(r"(?:increment|increments)[^\$]*\$(?P<increment>\d+(?:\.\d+)?)", re.IGNORECASE)
_OCC_UNDERLYING_RE = re.compile(r"^(?P<underlying>[A-Z]+)\d{6}[CP]\d{8}$")


def _extract_price_increment(message: str | None) -> float | None:
    if not message:
        return None
    match = _PRICE_INCREMENT_RE.search(message)
    if match is not None:
        parsed = _maybe_float(match.group("increment"))
        if parsed:
            return parsed
    lowered = message.lower()
    if "$" in lowered and "increment" in lowered:
        return DEFAULT_OPTION_TICK_INCREMENT
    return None


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except Exception:
            return str(exc)
        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                value = payload.get(key)
                if value:
                    return str(value)
    return str(exc)


def underlying_symbol_from_option(option_symbol: str) -> str | None:
    normalized = normalize_option_symbol(option_symbol)
    match = _OCC_UNDERLYING_RE.fullmatch(normalized)
    if match is not None:
        return match.group("underlying")
    return None
