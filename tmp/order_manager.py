"""
order_manager.py

Handles the execution of orders and broker interactions. This class contains the "doing" part
of the trading system, responsible for placing orders, checking status, and canceling orders.
"""
import asyncio
import logging
import httpx
import uuid
import re
from typing import Optional, Dict, Any, Callable
from src.brokers.shared import get_broker_client
from src.domain.trading.models import DomainOrder, Side, OrderType, TimeInForce, TradeState
from utils.pricing import round_price, get_price_for_entry, round_to_increment
from config import Config

# New imports for cache invalidation
from src.account import get_primary_account_id
from utils.cache_manager import get_cache_manager

logger = logging.getLogger(__name__)

# Cache for learned tick sizes - key: underlying symbol, value: tick increment
TICK_SIZE_CACHE = {}

# Default tick size for options (most common is $0.05)
DEFAULT_OPTION_TICK_SIZE = 0.05

class OrderManager:
    """
    Handles all broker interactions for order management.
    """

    def __init__(self):
        self.broker = get_broker_client()

    async def _place_order_with_retry(self, order: 'DomainOrder', pre_generated_order_id: Optional[str] = None,
                                      max_retries: int = 3) -> tuple[Optional[str], Optional[str]]:
        """
        Place an order with retry logic and idempotency support.
        
        Args:
            order: The domain order to place
            pre_generated_order_id: Pre-generated UUID for deduplication (optional)
            max_retries: Maximum number of retry attempts
            
        Returns:
            (order_id, error_message) tuple
        """
        # Generate order ID once for all retries (idempotency)
        order_id = pre_generated_order_id or str(uuid.uuid4())
        
        for attempt in range(max_retries):
            try:
                logger.debug(f"Placing order with ID {order_id} (attempt {attempt + 1}/{max_retries})")
                
                # Use the same order ID for all retry attempts
                response = await self.broker.place_order(order, pre_generated_order_id=order_id)
                
                # Extract order ID from response
                returned_order_id = await self._extract_order_id(response)
                
                if returned_order_id:
                    logger.info(f"Order placed successfully: {returned_order_id}")
                    return str(returned_order_id), None
                else:
                    error_msg = "Failed to place order - no order ID returned"
                    logger.error(error_msg)
                    return None, error_msg
                    
            except httpx.TimeoutException as e:
                logger.warning(f"Order placement timeout (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return None, f"Order placement timed out after {max_retries} attempts"
                # Continue with retry using same order ID
                await asyncio.sleep(1)  # Brief delay before retry
                
            except Exception as e:
                logger.error(f"Order placement failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return None, f"Order placement failed: {str(e)}"
                await asyncio.sleep(1)  # Brief delay before retry
        
        return None, "Order placement failed after all retry attempts"

    async def _send_order_with_retry(self, order_payload: Dict[str, Any], initial_price: float, symbol: str) -> tuple[Optional[str], Optional[str]]:
        """
        Send an order with reactive, self-correcting logic for tick size errors.

        Args:
            order_payload: The order payload dictionary to send
            initial_price: The initial calculated price before any rounding
            symbol: The full option symbol (e.g., "GDX250926P00068000")

        Returns:
            (order_id, error_message) tuple
        """
        underlying_symbol = await self._extract_underlying_symbol(symbol)

        if underlying_symbol in TICK_SIZE_CACHE:
            increment = TICK_SIZE_CACHE[underlying_symbol]
            corrected_price = round_to_increment(initial_price, increment)
            if 'limitPrice' in order_payload:
                order_payload['limitPrice'] = f"{corrected_price:.2f}"
                logger.debug(f"Applied known tick size rule for {underlying_symbol}: adjusted price {initial_price} -> {corrected_price}")
            elif 'stopPrice' in order_payload:
                order_payload['stopPrice'] = f"{corrected_price:.2f}"
                logger.debug(f"Applied known tick size rule for {underlying_symbol}: adjusted stop price {initial_price} -> {corrected_price}")

        try:
            response = await self.broker.place_order(order_payload)
            returned_order_id = await self._extract_order_id(response)
            if returned_order_id:
                logger.info(f"Order placed successfully: {returned_order_id}")
                return str(returned_order_id), None
            else:
                error_msg = "Failed to place order - no order ID returned"
                logger.error(error_msg)
                return None, error_msg

        except Exception as e:
            error_to_check = str(e)
            if isinstance(e, httpx.HTTPStatusError):
                try:
                    response_json = e.response.json()
                    error_to_check = response_json.get("message", str(e))
                except Exception:
                    pass

            if "$" in error_to_check.lower() and "increment" in error_to_check.lower():
                increment = None
                increment_match = re.search(r'increments of \$([0-9.]+)', error_to_check.lower())
                if increment_match:
                    try:
                        increment = float(increment_match.group(1))
                    except ValueError:
                        logger.warning(f"Failed to parse increment value from error: '{error_to_check}'. Falling back to default.")
                
                if not increment:
                    logger.warning(f"Could not parse increment from error message: '{error_to_check}'. Using default tick size {DEFAULT_OPTION_TICK_SIZE}.")
                    increment = DEFAULT_OPTION_TICK_SIZE

                TICK_SIZE_CACHE[underlying_symbol] = increment
                logger.info(f"Using tick size rule for {underlying_symbol}: {increment}")

                corrected_price = round_to_increment(initial_price, increment)

                if 'limitPrice' in order_payload:
                    order_payload['limitPrice'] = f"{corrected_price:.2f}"
                elif 'stopPrice' in order_payload:
                    order_payload['stopPrice'] = f"{corrected_price:.2f}"

                try:
                    response = await self.broker.place_order(order_payload)
                    returned_order_id = await self._extract_order_id(response)
                    if returned_order_id:
                        logger.info(f"Order placed successfully on retry: {returned_order_id}")
                        return str(returned_order_id), None
                    else:
                        error_msg = "Failed to place order on retry - no order ID returned"
                        logger.error(error_msg)
                        return None, error_msg
                except Exception as retry_e:
                    error_msg = f"Order placement failed on retry: {str(retry_e)}"
                    logger.error(error_msg)
                    return None, error_msg
            else:
                raise e

    async def _extract_underlying_symbol(self, option_symbol: str) -> str:
        """
        Extract the underlying stock symbol from an OCC option symbol.

        Args:
            option_symbol: OCC formatted option symbol (e.g., "GDX250926P00068000")

        Returns:
            The underlying symbol (e.g., "GDX")
        """
        # Find the position where the option type (P/C) and date begin
        match = re.search(r'^([A-Z]+)\d{6}[PC]', option_symbol)
        if match:
            return match.group(1)
        else:
            # Fallback: return the whole symbol if parsing fails
            logger.warning(f"Could not parse underlying symbol from {option_symbol}, using entire symbol")
            return option_symbol

    async def _extract_order_id(self, response: Any) -> Optional[str]:
        """Extract order ID from broker response, handling different formats."""
        if isinstance(response, dict):
            if "orderId" in response:
                return response["orderId"]
            elif "id" in response:
                return response["id"]
            else:
                logger.error(f"Could not extract order ID from response: {response}")
                return None
        elif isinstance(response, str):
            return response
        else:
            logger.error(f"Unexpected response type: {type(response)}, value: {response}")
            return None

    async def place_entry_order(self, resolved_symbol: str, entry_price: float, quantity: int,
                                trade_id: str = "UNKNOWN") -> tuple[Optional[str], Optional[str]]:
        """
        Place the initial entry order for a trade with tick size learning and correction.

        Args:
            resolved_symbol: The API-resolved symbol for the option
            entry_price: The calculated entry price
            quantity: Number of contracts to buy
            trade_id: Trade identifier for logging (optional)

        Returns:
            (order_id, error_message) tuple.
            - order_id: The order ID if successful, None if failed
            - error_message: Specific error message if failed, None if successful
        """
        try:
            logger.debug(f"Placing entry order for {resolved_symbol} (Trade: {trade_id})")

            # Validate inputs
            if not resolved_symbol:
                raise ValueError("Resolved symbol is required")
            if entry_price <= 0:
                raise ValueError(f"Invalid entry price: {entry_price}")
            if quantity <= 0:
                raise ValueError(f"Invalid quantity: {quantity}")

            # Build order payload dict
            from utils.symbols import normalize_option_symbol
            from datetime import datetime, timedelta, timezone
            sym = normalize_option_symbol(resolved_symbol)
            # Generate 30-day GTD expiration time (market close ET)
            gtd_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
                hour=20, minute=1, second=0, microsecond=0
            ).isoformat().replace("+00:00", "Z")
            order_payload = {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": sym, "type": "OPTION"},
                "orderSide": "BUY",
                "orderType": "LIMIT",
                "expiration": {
                    "timeInForce": "GTD",
                    "expirationTime": gtd_expiry
                },
                "quantity": str(int(quantity)),
                "openCloseIndicator": "OPEN",
                "limitPrice": f"{round_price(entry_price):.2f}"
            }

            # Use the reactive order placement with tick size correction
            order_id, error_msg = await self._send_order_with_retry(order_payload, entry_price, resolved_symbol)

            if order_id:
                logger.info(f"Entry order placed: {order_id} for {resolved_symbol} at {entry_price} (qty: {quantity})")
                # Invalidate cache for portfolio/orders so UI and backend see fresh state
                try:
                    account_id = await get_primary_account_id()
                    cache = get_cache_manager()
                    await cache.invalidate(f"portfolio_{account_id}")
                    await cache.invalidate_pattern(f"orders_{account_id}")
                except Exception as _e:
                    logger.debug(f"Cache invalidation failed after entry order: {_e}")

                return order_id, None
            else:
                logger.error(f"Failed to place entry order: {error_msg}")
                return None, error_msg

        except Exception as e:
            error_msg = f"Failed to place entry order: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return None, error_msg

    async def place_stop_loss_order(self, resolved_symbol: str, entry_price: float, quantity: int,
                                    trade_id: str = "UNKNOWN", custom_stop_price: float = None) -> tuple[Optional[str], Optional[str], Optional[float]]:
        """
        Place a stop loss order for a trade with tick size learning and correction.

        Args:
            resolved_symbol: The API-resolved symbol for the option
            entry_price: The entry price of the trade
            quantity: Number of contracts to sell
            trade_id: Trade identifier for logging (optional)
            custom_stop_price: Optional custom stop price. If provided, uses this instead of calculating from entry_price

        Returns:
            (order_id, error_message, stop_price) tuple.
            - order_id: The order ID if successful, None if failed
            - error_message: Specific error message if failed, None if successful
            - stop_price: The stop price used (for tracking purposes)
        """
        try:
            logger.debug(f"Placing stop loss order for {resolved_symbol} (Trade: {trade_id})")

            # Validate inputs
            if not resolved_symbol:
                raise ValueError("Resolved symbol is required")
            if entry_price <= 0:
                raise ValueError(f"Invalid entry price: {entry_price}")
            if quantity <= 0:
                raise ValueError(f"Invalid quantity: {quantity}")

            # Calculate stop price (use custom if provided, otherwise calculate)
            from utils.pricing import compute_stop_price
            from config import Config
            config = Config()

            if custom_stop_price is not None:
                stop_price = custom_stop_price
                logger.debug(f"Using custom stop price {stop_price} for trade {trade_id}")
            else:
                stop_price = compute_stop_price(entry_price, config.STOP_LOSS_PERCENTAGE)
                logger.debug(f"Calculated stop price {stop_price} for trade {trade_id}")

            # Build order payload dict (STOP order, CLOSE since we're selling an existing position)
            from utils.symbols import normalize_option_symbol
            from datetime import datetime, timedelta, timezone
            sym = normalize_option_symbol(resolved_symbol)
            # Generate 30-day GTD expiration time (market close ET)
            gtd_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
                hour=20, minute=1, second=0, microsecond=0
            ).isoformat().replace("+00:00", "Z")
            order_payload = {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": sym, "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "STOP",
                "expiration": {
                    "timeInForce": "GTD",
                    "expirationTime": gtd_expiry
                },
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
                "stopPrice": f"{round_price(stop_price):.2f}"
            }

            # Use the reactive order placement with tick size correction
            order_id, error_msg = await self._send_order_with_retry(order_payload, stop_price, resolved_symbol)

            if order_id:
                logger.info(f"Stop loss order placed: {order_id} for {resolved_symbol} at {stop_price} (qty: {quantity})")
                try:
                    account_id = await get_primary_account_id()
                    cache = get_cache_manager()
                    await cache.invalidate(f"portfolio_{account_id}")
                    await cache.invalidate_pattern(f"orders_{account_id}")
                except Exception as _e:
                    logger.debug(f"Cache invalidation failed after stop order: {_e}")

                return order_id, None, stop_price
            else:
                logger.error(f"Failed to place stop loss order: {error_msg}")
                return None, error_msg, None

        except Exception as e:
            error_msg = f"Failed to place stop loss order: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return None, error_msg, None

    async def check_order_status(self, order_id: str) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """
        Check the status of an order.
        Returns (status, error_message) tuple.
        - status: Order status if successful, None if failed
        - error_message: Specific error message if failed, None if successful
        """
        try:
            if not order_id:
                return None, None, "No order ID provided"

            order_data = await self.broker.get_order(order_id)
            # Use 'status' field instead of 'state' - this is what the Public API returns
            status = order_data.get('status', 'UNKNOWN') if order_data else 'UNKNOWN'
            # Return normalized status, raw payload and no error
            return status, order_data, None

        except Exception as e:
            error_msg = f"Failed to check order status: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return None, None, error_msg

    async def cancel_order(self, order_id: str) -> tuple[bool, Optional[str]]:
        """
        Cancel an order.
        Returns (success, error_message) tuple.
        """
        try:
            if not order_id:
                return False, "No order ID provided"

            await self.broker.cancel_order(order_id)
            # Broker cancel_order returns None on success, raises exception on failure
            logger.info(f"Order cancelled: {order_id}")

            # Invalidate cache so callers see updated orders/portfolio quickly
            try:
                account_id = await get_primary_account_id()
                cache = get_cache_manager()
                await cache.invalidate(f"portfolio_{account_id}")
                await cache.invalidate_pattern(f"orders_{account_id}")
            except Exception as _e:
                logger.debug(f"Cache invalidation failed after cancel: {_e}")

            return True, None

        except Exception as e:
            error_msg = f"Failed to cancel order: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return False, error_msg

    async def place_target_order(self, resolved_symbol: str, target_price: float, quantity: int,
                                target_level: int, trade_id: str = "UNKNOWN") -> tuple[Optional[str], Optional[str]]:
        """
        Place a target order (partial exit) with tick size learning and correction.

        Args:
            resolved_symbol: The API-resolved symbol for the option
            target_price: The target price for the limit order
            quantity: Number of contracts to sell
            target_level: Target level (1 or 2) for logging
            trade_id: Trade identifier for logging (optional)

        Returns:
            (order_id, error_message) tuple.
        """
        try:
            logger.debug(f"Placing target {target_level} order for {resolved_symbol} (Trade: {trade_id})")

            # Validate inputs
            if not resolved_symbol:
                raise ValueError("Resolved symbol is required")
            if target_price <= 0:
                raise ValueError(f"Invalid target price: {target_price}")
            if quantity <= 0:
                raise ValueError(f"Invalid quantity: {quantity}")
            if target_level not in [1, 2]:
                raise ValueError(f"Invalid target level: {target_level}")

            # Build order payload dict (LIMIT order, CLOSE since we're selling existing position)
            from utils.symbols import normalize_option_symbol
            from datetime import datetime, timedelta, timezone
            sym = normalize_option_symbol(resolved_symbol)
            # Generate 30-day GTD expiration time (market close ET)
            gtd_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
                hour=20, minute=1, second=0, microsecond=0
            ).isoformat().replace("+00:00", "Z")
            order_payload = {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": sym, "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "LIMIT",
                "expiration": {
                    "timeInForce": "GTD",
                    "expirationTime": gtd_expiry
                },
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE",
                "limitPrice": f"{round_price(target_price):.2f}"
            }

            # Use the reactive order placement with tick size correction
            order_id, error_msg = await self._send_order_with_retry(order_payload, target_price, resolved_symbol)

            if order_id:
                logger.info(f"Target {target_level} order placed: {order_id} for {resolved_symbol} at {target_price} (qty: {quantity})")
                try:
                    account_id = await get_primary_account_id()
                    cache = get_cache_manager()
                    await cache.invalidate(f"portfolio_{account_id}")
                    await cache.invalidate_pattern(f"orders_{account_id}")
                except Exception as _e:
                    logger.debug(f"Cache invalidation failed after target order: {_e}")

                return str(order_id), None
            else:
                error_msg = error_msg or f"Failed to place target {target_level} order - unknown error"
                logger.error(error_msg)
                return None, error_msg

        except Exception as e:
            error_msg = f"Failed to place target {target_level} order: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return None, error_msg

    async def place_square_off_order(self, resolved_symbol: str, quantity: int,
                                     trade_id: str = "UNKNOWN") -> tuple[Optional[str], Optional[str]]:
        """
        Place a market order to square off the position.

        Args:
            resolved_symbol: The API-resolved symbol for the option
            quantity: Number of contracts to sell
            trade_id: Trade identifier for logging (optional)

        Returns:
            (order_id, error_message) tuple.
        """
        try:
            logger.debug(f"Placing square off order for {resolved_symbol} (Trade: {trade_id})")

            # Validate inputs
            if not resolved_symbol:
                raise ValueError("Resolved symbol is required")
            if quantity <= 0:
                raise ValueError(f"Invalid quantity: {quantity}")

            # Build order payload dict (MARKET order, CLOSE since we're selling existing position)
            from utils.symbols import normalize_option_symbol
            sym = normalize_option_symbol(resolved_symbol)
            order_payload = {
                "orderId": str(uuid.uuid4()),
                "instrument": {"symbol": sym, "type": "OPTION"},
                "orderSide": "SELL",
                "orderType": "MARKET",
                "expiration": {"timeInForce": "DAY"},
                "quantity": str(int(quantity)),
                "openCloseIndicator": "CLOSE"
            }

            # Use the reactive order placement (market orders don't need tick size correction)
            order_id, error_msg = await self._send_order_with_retry(order_payload, 0.0, resolved_symbol)

            if order_id:
                logger.info(f"Square off order placed: {order_id} for {resolved_symbol} (qty: {quantity})")
                try:
                    account_id = await get_primary_account_id()
                    cache = get_cache_manager()
                    await cache.invalidate(f"portfolio_{account_id}")
                    await cache.invalidate_pattern(f"orders_{account_id}")
                except Exception as _e:
                    logger.debug(f"Cache invalidation failed after square-off order: {_e}")

                return str(order_id), None
            else:
                error_msg = error_msg or "Failed to place square off order - unknown error"
                logger.error(error_msg)
                return None, error_msg

        except Exception as e:
            error_msg = f"Failed to place square off order: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return None, error_msg
