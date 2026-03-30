import httpx
import logging
import json
import asyncio
import time
from typing import Optional, Dict, Any
from config import Config
from src.brokers.public.auth import get_access_token

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token bucket rate limiter for API requests.
    Ensures we don't exceed the configured rate limits.
    """
    
    def __init__(self, requests_per_second: float = 5.0, burst_limit: int = 10):
        self.requests_per_second = requests_per_second
        self.burst_limit = burst_limit
        self.tokens = burst_limit
        self.last_update = time.time()
        self.lock = asyncio.Lock()
        
        # Exponential backoff for 429 responses
        self.backoff_multiplier = 1.0
        self.max_backoff = 60.0  # Maximum 60 seconds
        self.base_backoff = 1.0   # Base 1 second
        
        # Monitoring
        self.request_count = 0
        self.rate_limit_count = 0
    
    async def acquire(self) -> None:
        """
        Acquire a token for making a request.
        Blocks until a token is available.
        """
        async with self.lock:
            now = time.time()
            # Add tokens based on time elapsed
            elapsed = now - self.last_update
            self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
            self.last_update = now
            
            if self.tokens < 1.0:
                # Calculate wait time
                wait_time = (1.0 - self.tokens) / self.requests_per_second
                logger.debug(f"Rate limit: waiting {wait_time:.2f}s for token")
                await asyncio.sleep(wait_time)
                # Recalculate after wait
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
                self.last_update = now
            
            # Only consume token if we have enough
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                self.request_count += 1
                logger.debug(f"Rate limit: {self.tokens:.1f} tokens remaining (total requests: {self.request_count})")
            else:
                # This shouldn't happen with the wait logic above, but safety check
                raise Exception("Rate limiter logic error: insufficient tokens")
    
    async def handle_429_response(self) -> None:
        """
        Handle 429 Too Many Requests response with exponential backoff.
        """
        async with self.lock:
            self.rate_limit_count += 1
            backoff_time = min(self.base_backoff * self.backoff_multiplier, self.max_backoff)
            logger.warning(f"Rate limit exceeded (429). Backing off for {backoff_time:.1f}s (total 429s: {self.rate_limit_count})")
            
            # Don't consume extra tokens during backoff - let the bucket refill naturally
            await asyncio.sleep(backoff_time)
            
            # Increase backoff multiplier (exponential)
            self.backoff_multiplier = min(self.backoff_multiplier * 2.0, 8.0)  # Cap at 8x
    
    def reset_backoff(self) -> None:
        """
        Reset backoff multiplier after successful requests.
        """
        self.backoff_multiplier = 1.0
    
    def get_stats(self) -> dict:
        """
        Get rate limiter statistics for monitoring.
        """
        return {
            'requests_per_second': self.requests_per_second,
            'burst_limit': self.burst_limit,
            'current_tokens': self.tokens,
            'backoff_multiplier': self.backoff_multiplier,
            'total_requests': self.request_count,
            'total_429s': self.rate_limit_count,
            'rate_limit_percentage': (self.rate_limit_count / max(self.request_count, 1)) * 100
        }


class PublicAPIClient:
    """
    A client for interacting with the Public.com API.
    This class is a singleton to reuse the httpx.AsyncClient.
    """
    _instance = None
    _client: httpx.AsyncClient = None
    _rate_limiter: RateLimiter = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(PublicAPIClient, cls).__new__(cls)
            cls._client = httpx.AsyncClient(
                base_url=Config.PUBLIC_API_BASE_URL.rstrip('/'),
                timeout=30.0  # 30 second timeout
            )
            # Initialize rate limiter with 5 req/sec limit
            cls._rate_limiter = RateLimiter(
                requests_per_second=getattr(Config, 'API_REQUESTS_PER_SECOND', 5.0),
                burst_limit=getattr(Config, 'API_BURST_LIMIT', 5)  # Allow small bursts
            )
        return cls._instance

    async def _get_headers(self) -> dict:
        """
        Constructs the required headers for an API request.
        """
        access_token = await get_access_token()
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

    async def get(self, endpoint: str, params: dict = None) -> dict:
        """
        Sends a GET request to the specified API endpoint.
        """
        await self._rate_limiter.acquire()
        
        try:
            headers = await self._get_headers()
            response = await self._client.get(endpoint, headers=headers, params=params)
            
            # Handle rate limiting
            if response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                # Don't retry immediately - let the backoff complete first
                # The caller should handle retries at the application level if needed
                response.raise_for_status()
            
            response.raise_for_status()
            self._rate_limiter.reset_backoff()  # Reset backoff on success
            return response.json()
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                raise Exception(f"API rate limit exceeded for GET {endpoint}") from e
            logger.error(f"API GET request to {e.request.url} failed with status {e.response.status_code}")
            logger.debug(f"Response body: {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"API GET request to {e.request.url} failed: {e}")
            raise

    async def post(self, endpoint: str, json_data: dict = None) -> dict:
        """
        Sends a POST request to the specified API endpoint.
        """
        await self._rate_limiter.acquire()
        
        headers = await self._get_headers()
        logger.debug(f"--- Sending POST Request ---")
        logger.debug(f"URL: {self._client.base_url}{endpoint}")
        if json_data:
            logger.debug(f"Payload keys: {list(json_data.keys()) if isinstance(json_data, dict) else 'non-dict payload'}")

        try:
            response = await self._client.post(endpoint, headers=headers, json=json_data)
            
            # Handle rate limiting
            if response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                # Don't retry immediately - let the backoff complete first
                response.raise_for_status()
            
            logger.debug(f"--- Received Response ---")
            logger.debug(f"Status Code: {response.status_code}")
            logger.debug(f"Response size: {len(response.text)} characters")

            response.raise_for_status()
            self._rate_limiter.reset_backoff()  # Reset backoff on success
            
            if response.status_code == 204: # No Content
                return {}
            
            return response.json()
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                raise Exception(f"API rate limit exceeded for POST {endpoint}") from e
            logger.error(f"API POST request to {e.request.url} failed with status {e.response.status_code}")
            logger.debug(f"Response body: {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"API POST request to {e.request.url} failed: {e}")
            raise

    async def delete(self, endpoint: str) -> dict:
        """
        Sends a DELETE request to the specified API endpoint.
        """
        await self._rate_limiter.acquire()
        
        headers = await self._get_headers()
        logger.debug(f"--- Sending DELETE Request ---")
        logger.debug(f"URL: {self._client.base_url}{endpoint}")
        
        try:
            response = await self._client.delete(endpoint, headers=headers)
            
            # Handle rate limiting
            if response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                # Don't retry immediately - let the backoff complete first
                response.raise_for_status()
            
            logger.debug(f"--- Received Response ---")
            logger.debug(f"Status Code: {response.status_code}")
            logger.debug(f"Response size: {len(response.text)} characters")

            response.raise_for_status()
            self._rate_limiter.reset_backoff()  # Reset backoff on success

            # Gracefully handle empty/No Content responses
            if not response.text or response.status_code in (202, 204):
                return {}

            # Try to parse JSON if present
            try:
                return response.json()
            except Exception:
                # Non-JSON but successful status
                return {"status": response.status_code, "body": response.text}
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                await self._rate_limiter.handle_429_response()
                raise Exception(f"API rate limit exceeded for DELETE {endpoint}") from e
            logger.error(f"API DELETE request to {e.request.url} failed with status {e.response.status_code}")
            logger.error(f"Response body: {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"API DELETE request to {e.request.url} failed: {e}")
            raise

    async def get_order_history(self, symbol: str = None, limit: int = 100) -> list:
        """
        Fetch order history from the broker.
        Used by reconciliation to check for closed orders.
        
        Args:
            symbol: Optional symbol filter (e.g., 'AAPL')
            limit: Maximum number of orders to return (default 100)
            
        Returns:
            List of order objects from the broker
        """
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
            
        try:
            response = await self.get("/orders", params=params)
            
            # Handle different response formats
            if isinstance(response, dict):
                orders = response.get("orders", [])
                if not isinstance(orders, list):
                    orders = [orders] if orders else []
            elif isinstance(response, list):
                orders = response
            else:
                logger.warning(f"Unexpected order history response format: {type(response)}")
                orders = []
                
            logger.debug(f"Fetched {len(orders)} orders from broker history")
            return orders
            
        except Exception as e:
            logger.error(f"Failed to fetch order history: {e}")
            return []

    async def close(self):
        """
        Closes the underlying httpx.AsyncClient.
        Should be called on application shutdown.
        """
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("PublicAPIClient session closed.")
