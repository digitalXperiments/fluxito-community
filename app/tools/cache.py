"""
Short-TTL cache layer for MCP tool reads.

Redis-backed cache for metadata reads (list_datasets, list_tables,
get_table_schema, list_properties, list_containers, audit results, etc.).
Falls back to a no-op if Redis is unavailable — caching is best-effort.

TTL tiers:
  META_TTL      300    (5 min)   — list_properties, list_containers, list_datasets
  SCHEMA_TTL    86400  (24 h)    — warehouse get_table_schema (rarely changes)
  AUDIT_TTL     3600   (1 h)     — audit/health checks (expensive, stable-ish)

Usage:
    from app.tools.cache import cached

    @cached(ttl=META_TTL, key_prefix="ga4:list_properties")
    async def list_properties(conn_id: str):
        ...

Or inline:
    key = build_key("bq:list_datasets", user_id, project_id)
    cached_val = await cache_get(key)
    if cached_val is not None:
        return cached_val
    result = await expensive_call()
    await cache_set(key, result, ttl=META_TTL)
    return result

Cache keys are namespaced with the user_id (or project_id) so data never
leaks across tenants.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

import app.app_state as state

logger = logging.getLogger(__name__)

# TTL tiers (seconds)
META_TTL = 300  # 5 min  — list_* metadata reads
SCHEMA_TTL = 86400  # 24 h   — table schemas
AUDIT_TTL = 3600  # 1 h    — audit/health checks

# Cache payload size limits (bytes)
_MAX_CACHE_SIZE = 256 * 1024  # 256 KB — skip caching huge payloads
_CACHE_KEY_PREFIX = "mcp:cache:"

# Substrings in exception messages that indicate an infra-level Redis failure
# rather than a benign per-key issue. Matches escalate to WARNING so the cache
# never dies silently in production.
_REDIS_CONNECTION_HINTS = ("connection", "timeout", "refused", "reset", "unreachable")


def _log_cache_error(op: str, key_or_prefix: str, err: Exception) -> None:
    msg = str(err).lower()
    if any(h in msg for h in _REDIS_CONNECTION_HINTS):
        logger.warning("%s: redis connectivity issue for %s: %s", op, key_or_prefix, err)
    else:
        logger.debug("%s failed for %s: %s", op, key_or_prefix, err)


def build_key(*parts: Any) -> str:
    """Build a stable cache key from arbitrary parts."""
    flat = [str(p) for p in parts if p is not None]
    raw = "|".join(flat)
    # Keep short readable prefix plus hash for overly long keys
    if len(raw) <= 200:
        return f"{_CACHE_KEY_PREFIX}{raw}"
    h = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"{_CACHE_KEY_PREFIX}{flat[0]}:{h}"


async def cache_get(key: str) -> Any | None:
    """
    Return cached JSON value, or None if cache miss or Redis unavailable.

    Gracefully handles Redis failures without raising exceptions to ensure
    cache is always optional (best-effort).
    """
    r = state.redis_client
    if not r:
        return None
    try:
        raw = await r.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    except Exception as e:
        _log_cache_error("cache_get", key, e)
        return None


async def cache_set(key: str, value: Any, ttl: int = META_TTL) -> None:
    """
    Store a JSON-serializable value with a TTL in Redis.

    Silently skips caching on failure, huge payloads, or if Redis is unavailable.
    This ensures caching is non-blocking and never raises exceptions.
    """
    r = state.redis_client
    if not r:
        return
    try:
        payload = json.dumps(value, default=str)
        # Skip caching huge payloads to avoid Redis pressure
        if len(payload) > _MAX_CACHE_SIZE:
            return
        await r.setex(key, ttl, payload)
    except Exception as e:
        _log_cache_error("cache_set", key, e)


async def cache_invalidate_prefix(prefix: str) -> int:
    """
    Delete all cache keys matching a prefix pattern.

    Used for write-through invalidation when data changes (e.g., after a
    dashboard write). Returns the number of keys deleted.
    """
    r = state.redis_client
    if not r:
        return 0
    try:
        deleted = 0
        async for key in r.scan_iter(match=f"{_CACHE_KEY_PREFIX}{prefix}*", count=200):
            await r.delete(key)
            deleted += 1
        return deleted
    except Exception as e:
        _log_cache_error("cache_invalidate_prefix", prefix, e)
        return 0


def cached(ttl: int = META_TTL, key_prefix: str = ""):
    """
    Decorator to cache an async function's result by its arguments and user.

    The user_id (from current user ctx) is automatically included in the
    cache key to prevent cross-tenant data leakage. Only caches successful
    (non-error) responses. Uses Redis with best-effort fallback to no-op.
    """

    def _decorator(fn: Callable):
        @wraps(fn)
        async def _wrapper(*args, **kwargs):
            try:
                user = state.current_user_ctx.get()
                uid = user.user_id if user else "anon"
            except Exception:
                uid = "anon"
            # Build a stable key from prefix + args + kwargs
            arg_key = hashlib.sha1(
                json.dumps([args, kwargs], default=str, sort_keys=True).encode()
            ).hexdigest()[:16]
            key = f"mcp:cache:{key_prefix}:{uid}:{arg_key}"
            r = state.redis_client
            if r:
                try:
                    raw = await r.get(key)
                    if raw is not None:
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        return json.loads(raw)
                except Exception:
                    pass
            result = await fn(*args, **kwargs)
            # Only cache successful (non-error) responses
            if isinstance(result, dict) and result.get("error"):
                return result
            if r:
                try:
                    payload = json.dumps(result, default=str)
                    if len(payload) <= _MAX_CACHE_SIZE:
                        await r.setex(key, ttl, payload)
                except Exception:
                    pass
            return result

        return _wrapper

    return _decorator
