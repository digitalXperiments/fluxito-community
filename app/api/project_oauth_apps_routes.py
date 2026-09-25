"""Per-project OAuth apps — a project brings its own developer app.

GET    /api/project/<slug>/oauth-apps                   — every platform + where it connects through
GET    /api/project/<slug>/oauth-apps/<platform>        — redirect URIs, console link, current state
POST   /api/project/<slug>/oauth-apps/<platform>        — save the project's own app
DELETE /api/project/<slug>/oauth-apps/<platform>        — remove it (falls back to the install app)
POST   /api/project/<slug>/oauth-apps/<platform>/test   — light syntax check of the saved app

Owners and admins of the project only. A project's own app is used for every
connect flow and token refresh in that project instead of the install-wide
app at /admin/oauth-apps. Tokens are bound to the app that issued them, so
switching a platform to a different app flags the project's existing
connections for it as needing a reconnect.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import app.app_state as app_state
from app.api.integrations_routes import DEV_CONSOLE_URL, PLATFORM_TUTORIALS, _redirect_uris_for
from app.api.project_routes import _get_membership, _get_project_by_slug, _resolve_user
from app.auth.oauth_app_credentials import (
    SUPPORTED_PLATFORMS,
    _mask,
    delete_project_oauth_app_credentials,
    get_project_oauth_app_credentials,
    list_project_oauth_app_status,
    mark_connections_for_reconnect,
    upsert_project_oauth_app_credentials,
)
from app.models.project import CAN_CONNECT_ROLES, Project

router = APIRouter()


async def _scope(request: Request, slug: str) -> tuple[dict, Project]:
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")
    membership = await _get_membership(project.id, uuid.UUID(user["user_id"]))
    if not membership:
        raise HTTPException(403, "You are not a member of this project")
    if membership.role not in CAN_CONNECT_ROLES:
        raise HTTPException(403, "Only owners and admins can manage the project's OAuth apps")
    return user, project


def _validate_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(400, f"Unsupported platform: {platform!r}")


def _project_redirect_uris(platform: str) -> list[str]:
    """Redirect URIs a project's own app needs.

    Signing in to Fluxito always goes through the install's Google app, so a
    project's Google app only needs the data-connection callback.
    """
    uris = _redirect_uris_for(platform)
    if platform == "google":
        uris = [u for u in uris if u.endswith("/auth/google/data/callback")]
    return uris


@router.get("/api/project/{slug}/oauth-apps")
async def list_project_oauth_apps(request: Request, slug: str):
    _, project = await _scope(request, slug)
    async with app_state.db_session_factory() as db:
        items = await list_project_oauth_app_status(db, project.id)
    return JSONResponse({"items": items})


@router.get("/api/project/{slug}/oauth-apps/{platform}")
async def get_project_oauth_app(request: Request, slug: str, platform: str):
    _validate_platform(platform)
    _, project = await _scope(request, slug)
    async with app_state.db_session_factory() as db:
        items = {i["platform"]: i for i in await list_project_oauth_app_status(db, project.id)}
    item = items[platform]
    return JSONResponse(
        {
            "platform": platform,
            "configured": item["source"] == "project",
            "source": item["source"],
            "client_id_masked": item["client_id_masked"],
            "redirect_uris": _project_redirect_uris(platform),
            "dev_console_url": DEV_CONSOLE_URL[platform],
            "tutorial_slugs": PLATFORM_TUTORIALS.get(platform, []),
        }
    )


@router.post("/api/project/{slug}/oauth-apps/{platform}")
async def save_project_oauth_app(request: Request, slug: str, platform: str):
    _validate_platform(platform)
    user, project = await _scope(request, slug)
    payload = await request.json()
    client_id = (payload.get("client_id") or "").strip()
    client_secret = (payload.get("client_secret") or "").strip()
    extra = payload.get("extra") or None

    if not client_id or not client_secret:
        raise HTTPException(400, "client_id and client_secret are required")
    if len(client_id) > 255:
        raise HTTPException(400, "client_id must be ≤ 255 chars")
    if len(client_secret) < 4:
        raise HTTPException(400, "client_secret looks too short")

    async with app_state.db_session_factory() as db:
        app_changed = await upsert_project_oauth_app_credentials(
            db,
            project_id=project.id,
            platform=platform,
            client_id=client_id,
            client_secret=client_secret,
            extra=extra if isinstance(extra, dict) else None,
            configured_by_user_id=uuid.UUID(user["user_id"]),
        )
        reconnect = (
            await mark_connections_for_reconnect(db, project_id=project.id, platform=platform)
            if app_changed
            else 0
        )
        await db.commit()

    return JSONResponse({"success": True, "platform": platform, "reconnect_count": reconnect})


@router.delete("/api/project/{slug}/oauth-apps/{platform}")
async def delete_project_oauth_app(request: Request, slug: str, platform: str):
    _validate_platform(platform)
    _, project = await _scope(request, slug)
    async with app_state.db_session_factory() as db:
        deleted = await delete_project_oauth_app_credentials(db, project_id=project.id, platform=platform)
        reconnect = (
            await mark_connections_for_reconnect(db, project_id=project.id, platform=platform)
            if deleted
            else 0
        )
        await db.commit()
    return JSONResponse({"success": True, "deleted": deleted, "reconnect_count": reconnect})


@router.post("/api/project/{slug}/oauth-apps/{platform}/test")
async def test_project_oauth_app(request: Request, slug: str, platform: str):
    _validate_platform(platform)
    _, project = await _scope(request, slug)
    async with app_state.db_session_factory() as db:
        creds = await get_project_oauth_app_credentials(db, platform, project.id)
    if creds is None:
        raise HTTPException(404, f"This project has no own {platform} app")

    issues: list[str] = []
    if len(creds.client_id) < 4:
        issues.append("client_id looks too short")
    if len(creds.client_secret) < 4:
        issues.append("client_secret looks too short")
    if platform == "google" and not (creds.extra or {}).get("developer_token"):
        issues.append("note: no Google Ads developer token (only required for Google Ads)")
    ok = not any(i for i in issues if not i.startswith("note:"))
    return JSONResponse(
        {
            "platform": platform,
            "ok": ok,
            "source": "project",
            "client_id_masked": _mask(creds.client_id),
            "issues": issues,
        }
    )
