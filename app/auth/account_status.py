"""Account status checks shared by browser sessions and MCP tokens.

Two kinds of deactivation:

* **Admin deactivation** (``deactivated_by_admin``) — every browser session and
  MCP token stops working immediately (well, within the short cache below) and
  only a super admin can lift it.
* **Self pause** (``is_active = false`` set by the user) — MCP tokens stop
  working; the browser session keeps working so the user can reactivate from
  their profile, and signing in again reactivates the account.

Lookups are cached in-process for a few seconds so the per-request check is
cheap; ``forget`` drops an entry immediately after a status change.
"""

from __future__ import annotations

import time
import uuid

_TTL_SEC = 10
_CACHE: dict[str, tuple[float, bool, bool]] = {}  # uid → (ts, is_active, deactivated_by_admin)


async def _status(user_id: str) -> tuple[bool, bool] | None:
    entry = _CACHE.get(user_id)
    if entry and time.monotonic() - entry[0] < _TTL_SEC:
        return entry[1], entry[2]
    from sqlalchemy import select

    import app.app_state as app_state
    from app.models.user import User

    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return None
    async with app_state.db_session_factory() as db:
        row = (
            await db.execute(select(User.is_active, User.deactivated_by_admin).where(User.id == uid))
        ).first()
    if row is None:
        return None
    _CACHE[user_id] = (time.monotonic(), bool(row[0]), bool(row[1]))
    return bool(row[0]), bool(row[1])


async def blocked_for_browser(user_id: str) -> bool:
    """True when a browser session for this user must be treated as signed out."""
    status = await _status(user_id)
    return status is None or status[1]


async def blocked_for_mcp(user_id: str) -> bool:
    """True when MCP tokens for this user must be refused (deactivated in any way)."""
    status = await _status(user_id)
    return status is None or not status[0] or status[1]


def forget(user_id: str) -> None:
    _CACHE.pop(str(user_id), None)


def clear() -> None:
    """Drop every cached status. Used by tests."""
    _CACHE.clear()
