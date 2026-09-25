"""Update detection: compare the running version against the latest GitHub release.

The latest-release lookup is cached in Redis to respect GitHub's unauthenticated
rate limit (60/hr) and avoid a network call on every page load.
"""

from __future__ import annotations

import json
import logging

import httpx

import app.app_state as app_state
from app._version import get_version
from app.settings_service import update_checks_enabled

logger = logging.getLogger(__name__)

GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/digitalXperiments/fluxito-community/releases/latest"
CACHE_KEY = "update:latest_release"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
HTTP_TIMEOUT = 5.0


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse 'vMAJOR.MINOR.PATCH' (with optional +/- suffix) into a comparable tuple."""
    cleaned = value.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = (cleaned.split(".") + ["0", "0", "0"])[:3]
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (0, 0, 0)


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a strictly higher semver than `current`."""
    return parse_semver(latest) > parse_semver(current)


async def _fetch_latest_release() -> dict | None:
    """Fetch the latest release JSON from GitHub. Returns the raw dict or None."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fluxito-update-check"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(GITHUB_LATEST_RELEASE_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _get_cached_or_fetch(force: bool = False) -> dict | None:
    """Return the latest-release payload from Redis cache, fetching + caching on miss.

    When force=True, skip the cache read but still write the fresh result back.
    """
    redis = app_state.redis_client
    if redis is not None and not force:
        cached = await redis.get(CACHE_KEY)
        if cached:
            return json.loads(cached)
    data = await _fetch_latest_release()
    if data is not None and redis is not None:
        slim = {
            "tag_name": data.get("tag_name"),
            "html_url": data.get("html_url"),
            "published_at": data.get("published_at"),
        }
        await redis.setex(CACHE_KEY, CACHE_TTL_SECONDS, json.dumps(slim))
        return slim
    return data


async def check_for_update(force: bool = False) -> dict:
    """Return update status. Never raises — all failures degrade to 'no update'.

    When force=True, bypass the Redis cache and re-poll GitHub.
    """
    current = get_version()
    base = {
        "current": current,
        "latest": None,
        "update_available": False,
        "release_notes_url": None,
        "published_at": None,
        "checks_enabled": True,
    }
    try:
        if not await update_checks_enabled():
            base["checks_enabled"] = False
            return base
        # Defense-in-depth: if the version is a non-release/local build
        # (CHANGELOG unreadable), we can't compare meaningfully — suppress the dot.
        if "+local" in current:
            return base
        payload = await _get_cached_or_fetch(force)
        if not payload or not payload.get("tag_name"):
            return base
        latest_raw = payload["tag_name"]
        latest = latest_raw.lstrip("vV")
        base["latest"] = latest
        base["release_notes_url"] = payload.get("html_url")
        base["published_at"] = payload.get("published_at")
        base["update_available"] = is_newer(latest_raw, current)
    except Exception:  # update check must never break the UI
        logger.warning("update check failed", exc_info=True)
    return base
