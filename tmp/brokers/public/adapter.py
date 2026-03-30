from __future__ import annotations
from typing import Dict, List, Optional
from enum import IntEnum
import asyncio
import uuid
import logging
from src.brokers.base import BrokerClient, Quote
from src.api_client import PublicAPIClient
from src.account import get_primary_account_id, get_account_portfolio
from src.market_data import get_stock_quote as md_stock_quote, get_option_quote as md_option_quote, get_option_chain as md_option_chain, get_option_greeks as md_option_greeks
from src.domain.trading.models import DomainOrder, OrderType, TimeInForce
from utils.symbols import normalize_option_symbol
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def _round_price(v: float) -> str:
    return f"{float(v):.2f}"


def _default_gtd_expiration_iso() -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=30)
    dt = dt.replace(hour=20, minute=1, second=0, microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _domain_to_public_order(order: DomainOrder, pre_generated_order_id: Optional[str] = None) -> dict[str, Any]:
    """
    Convert a domain order to Public.com API format.
    
    Args:
        order: Domain order object
        pre_generated_order_id: Pre-generated UUID for deduplication. If None, generates new one.
    
    Returns:
        Order payload for Public.com API
    """
    sym = normalize_option_symbol(order.symbol)
    exp: dict[str, str] = {"timeInForce": order.time_in_force.value}
    if order.time_in_force == TimeInForce.GTD:
        exp["expirationTime"] = _default_gtd_expiration_iso()

    # Use pre-generated order ID for deduplication, or generate new one
    order_id = pre_generated_order_id or str(uuid.uuid4())

    payload: dict[str, Any] = {
        "orderId": order_id,
        "instrument": {"symbol": sym, "type": "OPTION"},
        "orderSide": order.side.value,
        "orderType": order.type.value,
        "expiration": exp,
        "quantity": str(int(order.quantity)),
        "openCloseIndicator": (order.open_close or "OPEN").upper(),
    }

    if order.type == OrderType.LIMIT:
        if order.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        payload["limitPrice"] = _round_price(order.limit_price)
    elif order.type == OrderType.MARKET:
        # no extra fields
        pass
    elif order.type == OrderType.STOP:
        if order.stop_price is None:
            raise ValueError("STOP order requires stop_price")
        payload["stopPrice"] = _round_price(order.stop_price)
    elif order.type == OrderType.STOP_LIMIT:
        if order.stop_price is None:
            raise ValueError("STOP_LIMIT order requires stop_price")
        payload["stopPrice"] = _round_price(order.stop_price)
        # If caller provides limit_price use it, else default to 2% below stop
        limit = order.limit_price if order.limit_price is not None else float(order.stop_price) * (1 - 0.02)
        payload["limitPrice"] = _round_price(limit)
    else:
        raise ValueError(f"Unsupported order type: {order.type}")

    return payload


class ApiPriority(IntEnum):
    HIGH = 1   # Critical backend trading actions (place/cancel orders)
    MEDIUM = 2 # Actions originating from sheet reader
    LOW = 3    # Data refresh requests from frontend


class PublicBrokerClient(BrokerClient):
    def __init__(self):
        # Initialize the prioritized queue system
        self._api_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._worker_task: asyncio.Task = None
        self._pending_gets: Dict[str, asyncio.Future] = {}
        self._api_tasks: List[asyncio.Task] = []  # Track all created tasks

        # Start the worker task
        self._ensure_worker_task()

    def _ensure_worker_task(self):
        """Ensure the worker task is running."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._api_worker())
            self._api_tasks.append(self._worker_task)

    async def _api_worker(self):
        """Worker task that processes API requests from the priority queue."""
        while True:
            try:
                # Get the highest priority job (lower value = higher priority)
                priority, job = await self._api_queue.get()

                # Execute the API call
                try:
                    result = await job['call'](*job['args'], **job['kwargs'])
                    # Set the result on the future
                    job['future'].set_result(result)
                except Exception as e:
                    # Set the exception on the future
                    job['future'].set_exception(e)

                # Clean up deduplication entry for GET requests
                if job.get('is_get') and 'get_key' in job:
                    self._pending_gets.pop(job['get_key'], None)

            except asyncio.CancelledError:
                # Worker is being cancelled
                break
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.error(f"Error in API worker: {e}")

            # Proactive throttling: 250ms between requests (~4 requests/second)
            await asyncio.sleep(0.25)

    async def close(self):
        """Gracefully cancel all API tasks and wait for them to complete."""
        logger = logging.getLogger(__name__)
        logger.info("Shutting down PublicBrokerClient tasks")
        
        # Cancel all tracked tasks
        for task in self._api_tasks:
            if not task.done():
                task.cancel()
        
        # Wait for all tasks to complete
        if self._api_tasks:
            try:
                await asyncio.gather(*self._api_tasks, return_exceptions=True)
                logger.info("All PublicBrokerClient tasks shut down successfully")
            except asyncio.CancelledError:
                logger.info("PublicBrokerClient tasks cancelled")
        
        # Clear pending futures to prevent hanging awaits
        for future in self._pending_gets.values():
            if not future.done():
                future.cancel()
        self._pending_gets.clear()

    def _generate_get_key(self, method_name: str, args: tuple, kwargs: dict) -> str:
        """Generate a unique key for GET request deduplication."""
        # Convert args and kwargs to a hashable representation
        args_str = str(args) + str(sorted(kwargs.items()))
        return f"{method_name}:{args_str}"

    async def _queue_request(self, priority: ApiPriority, call: Callable, *args, **kwargs) -> Any:
        """
        Queue an API request with priority handling and deduplication for GET requests.

        Args:
            priority: Priority level for the request
            call: The API method to call (e.g., client.post, client.get)
            *args: Positional arguments for the call
            **kwargs: Keyword arguments for the call

        Returns:
            The result of the API call
        """
        # Check for GET request deduplication
        is_get = call.__name__ == 'get' if hasattr(call, '__name__') else False
        get_key = None

        if is_get:
            get_key = self._generate_get_key(args[0] if args else '', args[1:], kwargs)
            if get_key in self._pending_gets:
                # Return existing future for identical pending GET request
                return await self._pending_gets[get_key]
        else:
            # For non-GET requests, create fresh API client instance
            call = self._create_api_client_call(call)

        # Create a future for this request
        future: asyncio.Future = asyncio.Future()
        job = {
            'call': call,
            'args': args,
            'kwargs': kwargs,
            'future': future,
            'is_get': is_get,
            'get_key': get_key
        }

        # Add to deduplication tracking for GET requests
        if is_get and get_key:
            self._pending_gets[get_key] = future

        # Queue the job with priority (lower value = higher priority)
        await self._api_queue.put((priority.value, job))

        # Wait for the result (or exception)
        return await future

    def _create_api_client_call(self, method_wrapper) -> Callable:
        """Create a fresh API client call for non-GET requests."""
        # For most methods, create a new client instance
        def client_call(*args, **kwargs):
            client = PublicAPIClient()
            method_name = method_wrapper.__name__ if hasattr(method_wrapper, '__name__') else str(method_wrapper).split('.')[-1]
            method = getattr(client, method_name)
            return method(*args, **kwargs)
        return client_call

    # Broker interface methods with priority routing

    async def get_portfolio(self, priority: ApiPriority = ApiPriority.LOW) -> Dict:
        """Get portfolio information with priority routing."""
        account_id = await get_primary_account_id()
        client = PublicAPIClient()
        return await self._queue_request(priority, client.get, f"/userapigateway/trading/{account_id}/portfolio/v2")

    async def get_positions(self, priority: ApiPriority = ApiPriority.LOW) -> List[Dict]:
        """Get positions with priority routing."""
        pf = await self.get_portfolio(priority=priority)
        return pf.get("positions", []) or []

    async def get_orders(self, priority: ApiPriority = ApiPriority.LOW) -> List[Dict]:
        """Get orders list with priority routing."""
        account_id = await get_primary_account_id()
        client = PublicAPIClient()
        return await self._queue_request(priority, client.get, f"/userapigateway/trading/{account_id}/orders")

    async def get_order(self, order_id: str, priority: ApiPriority = ApiPriority.MEDIUM) -> Dict:
        """Get specific order details with priority routing."""
        account_id = await get_primary_account_id()
        client = PublicAPIClient()
        return await self._queue_request(priority, client.get, f"/userapigateway/trading/{account_id}/order/{order_id}")

    async def cancel_order(self, order_id: str, priority: ApiPriority = ApiPriority.HIGH) -> None:
        """Cancel order with priority routing."""
        account_id = await get_primary_account_id()
        client = PublicAPIClient()
        await self._queue_request(priority, client.delete, f"/userapigateway/trading/{account_id}/order/{order_id}")

    async def place_order(self, order: Dict | DomainOrder, pre_generated_order_id: Optional[str] = None,
                         priority: ApiPriority = ApiPriority.HIGH) -> Dict:
        """
        Place an order with optional pre-generated order ID for idempotency.

        Args:
            order: Order to place (dict or DomainOrder)
            pre_generated_order_id: Pre-generated UUID for deduplication/retries
            priority: Priority level for the API request

        Returns:
            Response from the broker API
        """
        account_id = await get_primary_account_id()
        client = PublicAPIClient()

        if isinstance(order, dict):
            payload = order
        else:
            payload = _domain_to_public_order(order, pre_generated_order_id)

        return await self._queue_request(priority, client.post, f"/userapigateway/trading/{account_id}/order", json_data=payload)

    # Market data methods - use queue but with deduplication for GETs

    async def get_stock_quote(self, symbol: str, priority: ApiPriority = ApiPriority.LOW) -> Optional[float]:
        return await md_stock_quote(symbol)

    async def get_option_quote(self, symbol: str, priority: ApiPriority = ApiPriority.LOW) -> Optional[Quote]:
        return await md_option_quote(symbol)

    async def get_option_chain(self, symbol: str, expiration: str, priority: ApiPriority = ApiPriority.LOW) -> Dict:
        return await md_option_chain(symbol, expiration)

    async def get_option_greeks(self, osi_option_symbol: str, priority: ApiPriority = ApiPriority.LOW) -> Dict:
        return await md_option_greeks(osi_option_symbol)
