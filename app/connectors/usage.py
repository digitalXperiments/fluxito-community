"""Per-project, per-connector API usage counters.

Counts the real upstream calls Fluxito makes on a project's behalf. Every cache
MISS in :func:`app.cache.cached_tool_response` is one upstream fetch, and the
cache key's prefix (``cache:<prefix>:...``) identifies the connector. Counts are
stored as daily Redis counters so the UI can sum any window.

Best-effort and fully non-blocking: a counter failure never affects a tool call,
and reads return ``{}`` rather than raising.

Coverage: only connectors whose reads flow through the cache layer are counted
here — GA4, GTM, Meta, Google Ads and the other ad platforms, Amplitude, Adobe.
Uncached paths (BigQuery, Redshift, Snowflake, Search Console, Bing, Marketo) are
not yet instrumented and read as "no data".
"""

from __future__ import annotations

import datetime as dt

from app import app_state

# Cache-key prefix (the ``<prefix>`` in ``cache:<prefix>:...``) → the connector
# key used in app.connectors.rate_limits.CATALOG.
_PREFIX_TO_CONNECTOR: dict[str, str] = {
    "ga4": "ga4",
    "gtm": "gtm",
    "ads": "google_ads",
    "meta": "meta_ads",
    "tiktok": "tiktok_ads",
    "snap": "snap_ads",
    "x": "x_ads",
    "reddit": "reddit_ads",
    "pinterest": "pinterest_ads",
    "linkedin": "linkedin_ads",
    "apple": "apple_ads",
    "amp": "amplitude",
    "mix": "mixpanel",
    "posthog": "posthog",
    "adobe": "adobe_analytics",
    "launch": "adobe_launch",
}

# Connectors this module can actually observe (the rest read as "no data").
INSTRUMENTED_CONNECTORS: frozenset[str] = frozenset(_PREFIX_TO_CONNECTOR.values())

_KEY = "usage:{pid}:{connector}:{day}"
_RETENTION_DAYS = 45


def connector_for_cache_key(cache_key: str) -> str | None:
    """Map a ``cache:<prefix>:...`` key to a connector key, or None."""
    parts = cache_key.split(":", 2)
    if len(parts) < 2 or parts[0] != "cache":
        return None
    return _PREFIX_TO_CONNECTOR.get(parts[1])


async def record_cache_miss(cache_key: str) -> None:
    """Count one upstream call for the connector behind ``cache_key``.

    Resolves the active project from the per-call context the tool hook sets.
    Never raises.
    """
    try:
        connector = connector_for_cache_key(cache_key)
        if not connector:
            return
        ctx = app_state.current_project_ctx.get()
        project_id = getattr(ctx, "project_id", None)
        redis = app_state.redis_client
        if not project_id or redis is None:
            return
        day = dt.datetime.utcnow().strftime("%Y%m%d")
        key = _KEY.format(pid=project_id, connector=connector, day=day)
        await redis.incr(key)
        await redis.expire(key, _RETENTION_DAYS * 86400)
    except Exception:
        return


async def usage_for(project_id: object, connector_keys: list[str], days: int = 30) -> dict[str, int]:
    """Sum each connector's daily counters over the last ``days`` (inclusive).

    Returns ``{connector_key: total_calls}``; connectors with no recorded calls
    are omitted. Never raises — returns ``{}`` on any error.
    """
    try:
        redis = app_state.redis_client
        if redis is None or not connector_keys:
            return {}
        pid = str(project_id)
        today = dt.datetime.utcnow().date()
        day_strs = [(today - dt.timedelta(days=n)).strftime("%Y%m%d") for n in range(max(1, days))]
        keys: list[str] = []
        owners: list[str] = []
        for ck in connector_keys:
            for ds in day_strs:
                keys.append(_KEY.format(pid=pid, connector=ck, day=ds))
                owners.append(ck)
        vals = await redis.mget(keys)
        totals: dict[str, int] = {}
        for ck, raw in zip(owners, vals, strict=False):
            if raw is None:
                continue
            try:
                totals[ck] = totals.get(ck, 0) + int(raw)
            except (TypeError, ValueError):
                continue
        return totals
    except Exception:
        return {}
