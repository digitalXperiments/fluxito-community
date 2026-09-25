"""
Response Caching Layer

Caches tool read results in Redis to reduce Google API calls.
Cache keys incorporate user_id + tool + params for isolation.

Usage:
    from app.cache import cached_tool_response

    result = await cached_tool_response(
        cache_key=f"ga4:report:{connection_id}:{property_id}:{start}:{end}",
        ttl=120,
        func=connector.run_report,
        connection_id=connection_id,
        property_id=property_id,
        ...
    )

TTL guidelines (seconds):
    - GA4 reports:           120   (data is delayed 24-48h anyway)
    - GA4 realtime:           30   (needs freshness)
    - GTM container data:    300   (rarely changes)
    - Google Ads campaigns:   60   (budget/spend data can change)
    - Account/property lists: 600  (almost never changes mid-session)
    - Audit results:         300   (compute-heavy, data doesn't change fast)
"""

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

import app.app_state as app_state

logger = logging.getLogger(__name__)

# Cache key constants for consistent naming
CACHE_PREFIX = "cache"
MAX_KEY_LENGTH = 200  # Redis key length best practice
HASH_LENGTH = 16  # First 16 chars of SHA256 hash


def _make_cache_key(prefix: str, *args, **kwargs) -> str:
    """Build a deterministic cache key from prefix + arguments.

    Long keys are hashed to stay under MAX_KEY_LENGTH for Redis efficiency.
    """
    parts = [prefix]
    for a in args:
        parts.append(str(a))
    for k, v in sorted(kwargs.items()):
        parts.append(f"{k}={v}")
    raw = ":".join(parts)
    # Hash long keys for Redis efficiency
    if len(raw) > MAX_KEY_LENGTH:
        digest = hashlib.sha256(raw.encode()).hexdigest()[:HASH_LENGTH]
        return f"{CACHE_PREFIX}:{prefix}:{digest}"
    return f"{CACHE_PREFIX}:{raw}"


async def cached_tool_response(
    cache_key: str,
    ttl: int,
    func: Callable,
    *args: Any,
    **kwargs: Any,
) -> dict:
    """Cache a tool response in Redis. Returns cached result if available,
    otherwise calls func(*args, **kwargs) and caches the result.

    Cache misses and Redis errors are silent — the tool call always succeeds.
    Only successful tool responses (no 'error' key) are cached.

    Args:
        cache_key: Unique Redis key for this result
        ttl: Time-to-live in seconds
        func: Async callable that returns the result dict
        *args, **kwargs: Arguments to pass to func
    """
    redis = app_state.redis_client

    # Try cache first (non-blocking on error)
    try:
        cached = await redis.get(cache_key)
        if cached:
            logger.debug(f"Cache HIT: {cache_key}")
            return json.loads(cached)
    except Exception as e:
        logger.debug(f"Cache read error (continuing anyway): {e}")

    # Cache miss → one real upstream call. Count it per project + connector
    # (best-effort; never blocks or breaks the tool call).
    try:
        from app.connectors import usage as connector_usage

        await connector_usage.record_cache_miss(cache_key)
    except Exception as e:
        logger.debug(f"Usage counter failed (non-blocking): {e}")

    # Call the actual function
    result = await func(*args, **kwargs)

    # Cache the result (fire-and-forget, don't block on errors)
    try:
        # Only cache successful results (no 'error' key in response)
        if isinstance(result, dict) and not result.get("error"):
            await redis.setex(cache_key, ttl, json.dumps(result, default=str))
            logger.debug(f"Cache SET: {cache_key} (TTL={ttl}s)")
    except Exception as e:
        logger.debug(f"Cache write failed (non-blocking): {e}")

    return result


async def invalidate_cache_pattern(pattern: str) -> int:
    """Invalidate all cache keys matching a pattern.

    Use after write operations to bust stale caches. Non-blocking on error.

    Example: await invalidate_cache_pattern("cache:ga4:*:properties/12345*")

    Args:
        pattern: Redis glob pattern (e.g., "cache:ga4:*")

    Returns:
        Number of keys deleted (0 if Redis error occurs)
    """
    redis = app_state.redis_client
    count = 0
    try:
        async for key in redis.scan_iter(match=pattern, count=100):
            await redis.delete(key)
            count += 1
        if count > 0:
            logger.debug(f"Cache invalidated {count} keys matching {pattern}")
    except Exception as e:
        logger.warning(f"Cache invalidation failed for pattern '{pattern}': {e}")
    return count
