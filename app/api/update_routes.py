"""Update status + trigger endpoints.

- GET  /api/updates/status — any authenticated user (drives the version badge).
- POST /api/updates/check  — super-admin only (force-poll GitHub, bypass cache).
- POST /api/updates/apply  — super-admin only (triggers the updater sidecar).
- GET  /api/updates/job    — super-admin only (updater job status passthrough).
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import app.app_state as app_state
from app.api.admin_routes import require_superadmin
from app.api.google_oauth_routes import _resolve_user_ctx
from app.services import update_service

logger = logging.getLogger(__name__)
router = APIRouter()

UPDATER_URL = os.environ.get("UPDATER_URL", "http://updater:9000")
UPDATER_TOKEN = os.environ.get("UPDATER_TOKEN", "")
CHECK_COOLDOWN_KEY = "update:check_cooldown"
CHECK_COOLDOWN_SECONDS = 30


def _updater_http_error(exc: httpx.HTTPStatusError) -> JSONResponse:
    """Translate updater-side HTTP failures into stable browser-facing categories."""
    status_code = exc.response.status_code
    if status_code == 401:
        return JSONResponse(
            {"error": "updater authentication failed", "code": "updater_auth_failed"},
            status_code=503,
        )
    if status_code == 409:
        return JSONResponse(
            {"error": "update already in progress", "code": "update_in_progress"},
            status_code=409,
        )
    return JSONResponse(
        {"error": "updater request failed", "code": "updater_error"},
        status_code=502,
    )


async def _trigger_updater(version: str, previous: str) -> dict:
    """POST the target version to the updater sidecar. Returns its JSON response."""
    headers = {"Authorization": f"Bearer {UPDATER_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{UPDATER_URL}/update",
            headers=headers,
            json={"version": version, "previous": previous},
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/api/updates/status")
async def update_status(request: Request):
    """Return current vs latest version + whether an update is available (auth required)."""
    if not await _resolve_user_ctx(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse(await update_service.check_for_update())


@router.post("/api/updates/check")
async def update_check(request: Request):
    """Super-admin: force a fresh GitHub poll, bypassing the 6h cache.

    Guarded by a short per-instance cooldown so the forced poll can't exhaust
    GitHub's unauthenticated rate limit. Degrades open if Redis is unavailable.
    """
    await require_superadmin(request)  # raises 401/403
    redis = app_state.redis_client
    if redis is not None:
        try:
            if await redis.get(CHECK_COOLDOWN_KEY):
                return JSONResponse(
                    {"error": "checked too recently", "code": "check_cooldown"},
                    status_code=429,
                )
            await redis.setex(CHECK_COOLDOWN_KEY, CHECK_COOLDOWN_SECONDS, "1")
        except Exception:  # cooldown is best-effort; never block a check on Redis
            logger.warning("update check cooldown guard failed", exc_info=True)
    return JSONResponse(await update_service.check_for_update(force=True))


@router.post("/api/updates/apply")
async def update_apply(request: Request):
    """Super-admin: trigger an update to the latest available version."""
    await require_superadmin(request)  # raises 401/403
    status = await update_service.check_for_update()
    if not status.get("update_available") or not status.get("latest"):
        return JSONResponse({"error": "no update available"}, status_code=409)
    try:
        result = await _trigger_updater(status["latest"], status["current"])
    except httpx.HTTPStatusError as exc:
        logger.error("updater rejected trigger: %s", exc)
        return _updater_http_error(exc)
    except httpx.RequestError as exc:
        logger.error("updater trigger failed: %s", exc)
        return JSONResponse(
            {"error": "updater unavailable", "code": "updater_unavailable"},
            status_code=503,
        )
    return JSONResponse({"status": "started", "target": status["latest"], "updater": result})


@router.get("/api/updates/job")
async def update_job(request: Request):
    """Super-admin: current updater job status (survives app restart via shared volume)."""
    await require_superadmin(request)
    headers = {"Authorization": f"Bearer {UPDATER_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{UPDATER_URL}/status", headers=headers)
            resp.raise_for_status()
            return JSONResponse(resp.json())
    except httpx.HTTPStatusError as exc:
        logger.warning("updater rejected status check: %s", exc)
        return _updater_http_error(exc)
    except httpx.RequestError as exc:
        # During the app's own restart the updater may briefly be unreachable.
        logger.warning("updater status check failed: %s", exc)
        return JSONResponse(
            {"status": "unknown", "error": "updater unavailable", "code": "updater_unavailable"},
            status_code=503,
        )
