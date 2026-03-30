"""
cache_manager.py

Server-side caching layer for broker API responses.
Provides intelligent caching with different TTLs for frontend vs backend usage.
"""
import asyncio
import time
import logging
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class CacheType(Enum):
    """Different cache types with varying freshness requirements."""
    FRONTEND_DISPLAY = "frontend_display"  # 30-60 seconds for UI
    BACKEND_DECISIONS = "backend_decisions"  # 5-10 seconds for trading logic
    STATIC_DATA = "static_data"  # Hours for rarely changing data

@dataclass
class CacheEntry:
    """Cache entry with metadata."""
    data: Any
    timestamp: float
    ttl: int
    cache_type: CacheType
    hit_count: int = 0

    @property
    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return time.time() - self.timestamp > self.ttl

    @property
    def age_seconds(self) -> float:
        """Get age of cache entry in seconds."""
        return time.time() - self.timestamp

class BrokerCacheManager:
    """
    Intelligent caching manager for broker API responses.

    Features:
    - Different TTLs for frontend vs backend usage
    - Automatic cache invalidation
    - Cache statistics and monitoring
    - Thread-safe operations
    """

    def __init__(self):
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'api_calls_saved': 0
        }

        # Default TTLs (configurable via environment)
        self._default_ttls = {
            CacheType.FRONTEND_DISPLAY: 45,  # 45 seconds for UI
            CacheType.BACKEND_DECISIONS: 8,   # 8 seconds for trading decisions
            CacheType.STATIC_DATA: 3600       # 1 hour for static data
        }

    async def get_or_fetch(
        self,
        cache_key: str,
        fetch_func: Callable[[], Any],
        cache_type: CacheType = CacheType.FRONTEND_DISPLAY,
        force_refresh: bool = False
    ) -> Any:
        """
        Get data from cache or fetch fresh data.

        Args:
            cache_key: Unique key for the cached data
            fetch_func: Async function to fetch fresh data
            cache_type: Type of cache usage (determines TTL)
            force_refresh: Force fresh fetch even if cache is valid

        Returns:
            Cached or freshly fetched data
        """
        # First, attempt to read from cache under lock
        async with self._lock:
            if not force_refresh and cache_key in self._cache:
                entry = self._cache[cache_key]
                if not entry.is_expired:
                    entry.hit_count += 1
                    self._stats['hits'] += 1
                    logger.debug(f"Cache HIT for {cache_key} (age: {entry.age_seconds:.1f}s, hits: {entry.hit_count})")
                    return entry.data

            # Cache miss or expired - we'll fetch fresh data (do not hold lock while awaiting)
            self._stats['misses'] += 1
            logger.debug(f"Cache MISS for {cache_key} - will fetch fresh data")

        # Perform the potentially long-running fetch outside the lock to avoid deadlocks
        try:
            fresh_data = await fetch_func()
        except Exception as e:
            logger.error(f"Failed to fetch fresh data for {cache_key}: {e}")
            # If there is stale data available, return it
            async with self._lock:
                if cache_key in self._cache and not force_refresh:
                    stale_entry = self._cache[cache_key]
                    logger.warning(f"Returning stale cache data for {cache_key} (age: {stale_entry.age_seconds:.1f}s)")
                    return stale_entry.data
            raise

        ttl = self._default_ttls[cache_type]

        # Store the fresh data under lock, but handle the race where another coroutine
        # may have updated the cache while we were fetching.
        async with self._lock:
            existing = self._cache.get(cache_key)
            if existing is None or existing.is_expired or force_refresh:
                self._cache[cache_key] = CacheEntry(
                    data=fresh_data,
                    timestamp=time.time(),
                    ttl=ttl,
                    cache_type=cache_type
                )
                logger.debug(f"Cached fresh data for {cache_key} with TTL {ttl}s")
                # Account saved API call
                self._stats['api_calls_saved'] += 1
                return fresh_data
            else:
                # Another coroutine already refreshed the cache; return the stored value
                logger.debug(f"Cache was updated by another coroutine for {cache_key}; returning stored data")
                return self._cache[cache_key].data

    async def invalidate(self, cache_key: str) -> bool:
        """
        Invalidate a specific cache entry.

        Returns True if entry was invalidated, False if not found.
        """
        async with self._lock:
            if cache_key in self._cache:
                del self._cache[cache_key]
                self._stats['evictions'] += 1
                logger.debug(f"Invalidated cache entry: {cache_key}")
                return True
            return False

    async def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate all cache entries matching a pattern.

        Returns number of entries invalidated.
        """
        async with self._lock:
            keys_to_remove = [k for k in self._cache.keys() if pattern in k]
            for key in keys_to_remove:
                del self._cache[key]
                self._stats['evictions'] += 1
            logger.debug(f"Invalidated {len(keys_to_remove)} cache entries matching '{pattern}'")
            return len(keys_to_remove)

    async def set(
        self,
        cache_key: str,
        data: Any,
        ttl: Optional[int] = None,
        cache_type: CacheType = CacheType.FRONTEND_DISPLAY
    ) -> None:
        """
        Store data directly in cache without fetching.

        Args:
            cache_key: Unique key for the cached data
            data: Data to cache
            ttl: Time-to-live in seconds (defaults to cache_type default)
            cache_type: Type of cache usage (determines default TTL)
        """
        if ttl is None:
            ttl = self._default_ttls[cache_type]

        async with self._lock:
            self._cache[cache_key] = CacheEntry(
                data=data,
                timestamp=time.time(),
                ttl=ttl,
                cache_type=cache_type
            )
            self._stats['api_calls_saved'] += 1
            logger.debug(f"Direct cache SET for {cache_key} with TTL {ttl}s")

    async def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        async with self._lock:
            total_entries = len(self._cache)
            expired_entries = sum(1 for entry in self._cache.values() if entry.is_expired)

            return {
                **self._stats,
                'total_entries': total_entries,
                'expired_entries': expired_entries,
                'active_entries': total_entries - expired_entries,
                'hit_rate': self._stats['hits'] / max(self._stats['hits'] + self._stats['misses'], 1) * 100,
                'entries': [
                    {
                        'key': k,
                        'age': v.age_seconds,
                        'ttl': v.ttl,
                        'hits': v.hit_count,
                        'expired': v.is_expired,
                        'type': v.cache_type.value
                    }
                    for k, v in self._cache.items()
                ]
            }

    async def cleanup_expired(self) -> int:
        """Remove expired cache entries. Returns number cleaned up."""
        async with self._lock:
            expired_keys = [k for k, v in self._cache.items() if v.is_expired]
            for key in expired_keys:
                del self._cache[key]
                self._stats['evictions'] += 1
            if expired_keys:
                logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")
            return len(expired_keys)

# Global cache manager instance
_cache_manager = None

def get_cache_manager() -> BrokerCacheManager:
    """Get the global cache manager instance."""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = BrokerCacheManager()
    return _cache_manager
