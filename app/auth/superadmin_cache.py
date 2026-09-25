"""TTL-cached super-admin lookup, used to exempt super-admins from rate limiting
without a per-request DB hit (and without threading the flag through the MCP
session context)."""

from __future__ import annotations

import logging
import time
import uuid

import app.app_state as app_state

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[bool, float]] = {}
_TTL = 60.0  # seconds


def _clear_superadmin_cache() -> None:
    """Test/maintenance helper — drop all cached entries."""
    _CACHE.clear()


async def is_superadmin_cached(user_id: str) -> bool:
    """Return whether *user_id* is a super-admin, cached for ~60s.

    Fail-open to False on any error (a DB blip must not grant exemption, and
    returning False simply means the normal rate-limit check runs).
    """
    now = time.time()
    hit = _CACHE.get(user_id)
    if hit is not None and (now - hit[1]) < _TTL:
        return hit[0]

    value = False
    try:
        from sqlalchemy import select

        from app.models.user import User

        async with app_state.db_session_factory() as db:
            u = (await db.execute(select(User).where(User.id == uuid.UUID(user_id)))).scalar_one_or_none()
            value = bool(u.is_superadmin) if u else False
    except Exception as e:
        logger.warning("is_superadmin_cached lookup failed for %s: %s", user_id, e)
        return False  # do not cache failures

    _CACHE[user_id] = (value, now)
    return value
