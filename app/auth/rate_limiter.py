"""
Per-user rate limiting via Redis — single flat limit for all users.

The limit is configurable at runtime via the admin panel.
Admin overrides are stored in Redis key `rate_limits:config` as JSON.
When no override exists, falls back to settings (env vars / .env).

Default: 60 req/min, 1000 req/hour

Usage:
    from app.auth.rate_limiter import check_rate_limit
    blocked = await check_rate_limit(user_id)
    if blocked:
        return blocked
"""

import json
import logging
import time

import app.app_state as app_state
from app.config import settings
from app.settings_service import get_runtime_setting

logger = logging.getLogger(__name__)

# Redis key constants
_RATE_LIMITS_CONFIG_KEY = "rate_limits:config"
_RATE_LIMIT_MINUTE_KEY_TEMPLATE = "rl:{user_id}:{minute_slot}"
_RATE_LIMIT_HOUR_KEY_TEMPLATE = "rl_h:{user_id}:{hour_slot}"
_RATE_LIMIT_DAY_KEY_TEMPLATE = "rl_d:{user_id}:{day_slot}"

# In-memory cache of limits (refreshed from Redis every 60s)
_cached_limits: dict | None = None
_cached_limits_ts: float = 0
_CACHE_TTL = 60  # seconds
_RATE_LIMIT_MINUTE_TTL = 120  # seconds, expire minute bucket after 2 minutes
_RATE_LIMIT_HOUR_TTL = 7200  # seconds, expire hour bucket after 2 hours
_RATE_LIMIT_DAY_TTL = 172800  # seconds, expire day bucket after 2 days


async def _default_limits() -> dict:
    """Return DB-backed default rate limits with env/default fallback."""
    per_min = settings.RATE_LIMIT_PER_MIN
    per_hour = settings.RATE_LIMIT_PER_HOUR
    per_day = settings.RATE_LIMIT_PER_DAY
    session_factory = getattr(app_state, "db_session_factory", None)
    if session_factory is not None:
        try:
            async with session_factory() as db:
                per_min = await get_runtime_setting(db, "rate_limit_per_min")
                per_hour = await get_runtime_setting(db, "rate_limit_per_hour")
                per_day = await get_runtime_setting(db, "rate_limit_per_day")
        except Exception as e:
            logger.warning("Failed to load DB rate limit settings; using env/default fallback: %s", e)
    return {"default": {"per_min": int(per_min), "per_hour": int(per_hour), "per_day": int(per_day)}}


async def get_rate_limits() -> dict:
    """
    Get the current rate limits (admin override > env defaults).
    Results are cached in-memory for 60s to avoid Redis round-trips on every request.
    """
    global _cached_limits, _cached_limits_ts

    now = time.time()
    if _cached_limits and (now - _cached_limits_ts) < _CACHE_TTL:
        return _cached_limits

    defaults = await _default_limits()

    try:
        redis = app_state.redis_client
        if redis:
            raw = await redis.get(_RATE_LIMITS_CONFIG_KEY)
            if raw:
                overrides = json.loads(raw)
                # Merge: overrides take precedence, but fill in any missing keys
                if "default" in overrides and isinstance(overrides["default"], dict):
                    defaults["default"]["per_min"] = overrides["default"].get(
                        "per_min", defaults["default"]["per_min"]
                    )
                    defaults["default"]["per_hour"] = overrides["default"].get(
                        "per_hour", defaults["default"]["per_hour"]
                    )
                    defaults["default"]["per_day"] = overrides["default"].get(
                        "per_day", defaults["default"]["per_day"]
                    )
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("Failed to read rate limit overrides from Redis: %s", e)

    _cached_limits = defaults
    _cached_limits_ts = now
    return defaults


async def set_rate_limits(limits: dict) -> None:
    """
    Save admin-configured rate limits to Redis.
    Expects: {"default": {"per_min": N, "per_hour": N}}
    Also invalidates the in-memory cache.
    """
    global _cached_limits, _cached_limits_ts

    redis = app_state.redis_client
    try:
        await redis.set(_RATE_LIMITS_CONFIG_KEY, json.dumps(limits))
        _cached_limits = None
        _cached_limits_ts = 0
        logger.info("Rate limits updated by admin: %s", limits)
    except Exception as e:
        logger.error("Failed to save rate limits to Redis: %s", e)
        raise


async def check_rate_limit(user_id: str, tier: str | None = None) -> dict | None:
    """
    Check if user has exceeded their rate limit.
    Returns None if allowed, or an error dict if rate-limited.
    The ``tier`` parameter is accepted for call-site compatibility but ignored —
    all users share the same flat limit configured via RATE_LIMIT_PER_MIN /
    RATE_LIMIT_PER_HOUR (or the admin Redis override).
    """
    limits = await get_rate_limits()
    tier_limits = limits["default"]

    per_min = tier_limits["per_min"]
    per_hour = tier_limits["per_hour"]
    per_day = tier_limits.get("per_day", settings.RATE_LIMIT_PER_DAY)

    redis = app_state.redis_client
    now = int(time.time())

    # Per-minute check
    minute_slot = now // 60
    minute_key = _RATE_LIMIT_MINUTE_KEY_TEMPLATE.format(user_id=user_id, minute_slot=minute_slot)
    try:
        minute_count = await redis.incr(minute_key)
        if minute_count == 1:
            await redis.expire(minute_key, _RATE_LIMIT_MINUTE_TTL)
    except Exception as e:
        logger.warning("Redis error checking minute rate limit for user %s: %s", user_id, e)
        return None  # Fail open: allow request on Redis failure

    if minute_count > per_min:
        logger.warning(
            "Rate limit hit (per-minute) for user %s: %d/%d",
            user_id,
            minute_count,
            per_min,
        )
        return {
            "error": True,
            "error_type": "rate_limited",
            "message": (
                f"Too many requests — you've made {minute_count} requests in the last minute "
                f"(limit: {per_min}/min)."
            ),
            "retry_after_seconds": 60 - (now % 60),
            "limit_per_min": per_min,
            "limit_per_hour": per_hour,
        }

    # Per-hour check
    hour_slot = now // 3600
    hour_key = _RATE_LIMIT_HOUR_KEY_TEMPLATE.format(user_id=user_id, hour_slot=hour_slot)
    try:
        hour_count = await redis.incr(hour_key)
        if hour_count == 1:
            await redis.expire(hour_key, _RATE_LIMIT_HOUR_TTL)
    except Exception as e:
        logger.warning("Redis error checking hour rate limit for user %s: %s", user_id, e)
        return None  # Fail open: allow request on Redis failure

    if hour_count > per_hour:
        logger.warning(
            "Rate limit hit (per-hour) for user %s: %d/%d",
            user_id,
            hour_count,
            per_hour,
        )
        return {
            "error": True,
            "error_type": "rate_limited",
            "message": (
                f"Hourly rate limit reached ({hour_count}/{per_hour} requests). "
                f"Limit resets in {3600 - (now % 3600)} seconds."
            ),
            "retry_after_seconds": 3600 - (now % 3600),
            "limit_per_min": per_min,
            "limit_per_hour": per_hour,
            "limit_per_day": per_day,
        }

    # Per-day check
    day_slot = now // 86400
    day_key = _RATE_LIMIT_DAY_KEY_TEMPLATE.format(user_id=user_id, day_slot=day_slot)
    try:
        day_count = await redis.incr(day_key)
        if day_count == 1:
            await redis.expire(day_key, _RATE_LIMIT_DAY_TTL)
    except Exception as e:
        logger.warning("Redis error checking day rate limit for user %s: %s", user_id, e)
        return None  # Fail open: allow request on Redis failure

    if day_count > per_day:
        logger.warning(
            "Rate limit hit (per-day) for user %s: %d/%d",
            user_id,
            day_count,
            per_day,
        )
        return {
            "error": True,
            "error_type": "rate_limited",
            "message": (
                f"Daily rate limit reached ({day_count}/{per_day} requests). "
                f"Limit resets in {86400 - (now % 86400)} seconds."
            ),
            "retry_after_seconds": 86400 - (now % 86400),
            "limit_per_min": per_min,
            "limit_per_hour": per_hour,
            "limit_per_day": per_day,
        }

    return None  # Rate limit check passed
