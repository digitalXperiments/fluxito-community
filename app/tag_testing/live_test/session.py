"""
Live Tag Test — Session Manager
================================

Manages LTT (Live Tag Test) sessions.

Sessions are stored in Redis (if available) for multi-worker safety, with
an in-memory fallback for development. TTL is 2 hours.

Session shape:
  {
    session_id:  str,
    project_id:  str,
    url:         str,
    status:      "active" | "complete" | "error",
    captures:    list[dict],   # raw network capture groups from Claude
    findings:    list[dict],   # validated findings
    sdr_context: dict | None,
    created_at:  str (ISO 8601)
  }
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

import app.app_state as state

logger = logging.getLogger(__name__)

_SESSION_TTL_SECONDS = 7200  # 2 hours
_SESSION_KEY_PREFIX = "ltt:session:"

# In-memory fallback (single-worker dev mode)
_memory_store: dict[str, dict] = {}
_memory_expiry: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_session(project_id: str, url: str, sdr_context: dict | None = None) -> str:
    """
    Create a new live tag test session.

    Returns the ``session_id`` (format: ``ltt_<uuid4_hex[:16]>``).
    """
    session_id = "ltt_" + uuid.uuid4().hex[:16]
    session: dict = {
        "session_id": session_id,
        "project_id": project_id,
        "url": url,
        "status": "active",
        "captures": [],
        "findings": [],
        "sdr_context": sdr_context,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await _store_session(session_id, session)
    return session_id


async def _load_owned(session_id: str) -> dict | None:
    """Load a session only if it belongs to the active project of the tool call."""
    session = await _load_session(session_id)
    if session is None:
        return None
    try:
        import app.app_state as _state

        proj = _state.current_project_ctx.get()
    except LookupError:
        proj = None
    active = str(getattr(proj, "project_id", "") or "") if proj else ""
    if active and str(session.get("project_id") or "") != active:
        return None
    return session


async def get_session(session_id: str) -> dict | None:
    """Retrieve a session by ID. Returns None if not found, expired or another project's."""
    return await _load_owned(session_id)


async def update_session(session_id: str, updates: dict) -> None:
    """Merge ``updates`` into the session and re-persist."""
    session = await _load_owned(session_id)
    if session is None:
        logger.warning(f"update_session: session '{session_id}' not found")
        return
    session.update(updates)
    session["updated_at"] = datetime.now(UTC).isoformat()
    await _store_session(session_id, session)


async def finish_session(session_id: str) -> dict:
    """
    Mark the session as complete, compute summary stats, and return the
    full session dict.
    """
    session = await _load_owned(session_id)
    if session is None:
        return {
            "error": True,
            "error_type": "not_found",
            "message": f"Session '{session_id}' not found or expired.",
        }

    session["status"] = "complete"
    session["finished_at"] = datetime.now(UTC).isoformat()

    # Compute summary
    findings = session.get("findings") or []
    session["summary"] = _compute_summary(findings)

    await _store_session(session_id, session)
    return session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_summary(findings: list[dict]) -> dict:
    critical = sum(1 for f in findings if f.get("status") == "critical")
    warning = sum(1 for f in findings if f.get("status") == "warning")
    info = sum(1 for f in findings if f.get("status") == "info")
    passed = sum(1 for f in findings if f.get("status") == "pass")

    from app.tag_testing.rule_books.validator import compute_score

    score = compute_score(critical, warning, info, passed)

    return {
        "critical": critical,
        "warning": warning,
        "info": info,
        "passed": passed,
        "score": score,
        "total_findings": len(findings),
    }


async def _store_session(session_id: str, session: dict) -> None:
    key = _SESSION_KEY_PREFIX + session_id
    payload = json.dumps(session, default=str)

    redis = state.redis_client if hasattr(state, "redis_client") else None
    if redis:
        try:
            await redis.setex(key, _SESSION_TTL_SECONDS, payload)
            return
        except Exception as e:
            logger.debug(f"Redis session write failed, using memory: {e}")

    # Memory fallback
    _memory_store[key] = session
    _memory_expiry[key] = time.monotonic() + _SESSION_TTL_SECONDS


async def _load_session(session_id: str) -> dict | None:
    key = _SESSION_KEY_PREFIX + session_id

    redis = state.redis_client if hasattr(state, "redis_client") else None
    if redis:
        try:
            raw = await redis.get(key)
            if raw:
                return json.loads(raw)
            return None
        except Exception as e:
            logger.debug(f"Redis session read failed, falling back to memory: {e}")

    # Memory fallback
    if key in _memory_store:
        if time.monotonic() > _memory_expiry.get(key, 0):
            del _memory_store[key]
            del _memory_expiry[key]
            return None
        return _memory_store[key]

    return None
