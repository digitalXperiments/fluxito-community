"""Reddit Ads OAuth routes."""

from __future__ import annotations

from html import escape as _html_escape
import asyncio
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, update

import app.app_state as app_state
from app.api.google_oauth_routes import (
    _encrypt,
    _render_interstitial,
    get_uid_from_request,
)
from app.auth.mcp_session_manager import (
    build_user_context,
    invalidate_user_context_cache,
    require_valid_mcp_token,
)
from app.models.connection import OAuthConnection

router = APIRouter(tags=["reddit-oauth"])

_SCOPES = ["identity", "adsread", "history"]


async def _load_user_ctx(request: Request):
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass
    return user_ctx


async def _reddit_creds(project_id: str | None = None):
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as db:
        return await get_oauth_app_credentials(db, "reddit", project_id=project_id)


def _callback_url(request: Request) -> str:
    from app.utils import base_url_from_request

    return f"{base_url_from_request(request).rstrip('/')}/auth/reddit/callback"


@router.get("/connect/reddit", response_class=HTMLResponse)
async def connect_reddit_page(request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/reddit", status_code=302)

    state = secrets.token_urlsafe(32)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    await app_state.redis_client.setex(
        f"reddit_oauth_state:{state}",
        600,
        json.dumps(
            {
                "user_id": user_ctx.user_id,
                "project_id": active_project_id,
            }
        ),
    )

    creds = await _reddit_creds(active_project_id)
    authorize_url = "https://www.reddit.com/api/v1/authorize?" + urlencode(
        {
            "client_id": creds.client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": _callback_url(request),
            "duration": "permanent",
            "scope": " ".join(_SCOPES),
        }
    )

    return await _render_interstitial(
        request=request,
        platform_slug="reddit",
        platform_name="Reddit Ads",
        platform_desc="Connect your Reddit Ads account to read ad account and campaign data.",
        btn_bg="linear-gradient(135deg,#FF4500,#cc3700)",
        btn_text_color="#fff",
        btn_shadow="0 4px 20px rgba(255,69,0,0.30)",
        permissions=[
            (
                "Ads",
                "adsread",
                "Read Reddit Ads accounts, campaigns, ad groups, ads, and reporting data.",
            ),
            (
                "History",
                "history",
                "Read account history endpoints used for audit and account-change context.",
            ),
        ],
        authorize_url=authorize_url,
        user_ctx=user_ctx,
    )


@router.get("/auth/reddit/callback")
async def reddit_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    if error:
        return RedirectResponse(f"/home?error=reddit_{error}", status_code=302)
    if not code or not state:
        return HTMLResponse("<h2>Invalid Reddit callback - missing code or state.</h2>", status_code=400)

    raw = await app_state.redis_client.get(f"reddit_oauth_state:{state}")
    if not raw:
        return HTMLResponse("<h2>Invalid or expired Reddit OAuth state.</h2>", status_code=400)
    await app_state.redis_client.delete(f"reddit_oauth_state:{state}")
    state_data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)

    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, state_data.get("user_id"), state_data.get("project_id"))
    if _denied is not None:
        return _denied

    creds = await _reddit_creds(state_data.get("project_id"))
    headers = {"User-Agent": "Fluxito:reddit-ads:v1.0"}
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _callback_url(request),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data=token_data,
            auth=(creds.client_id, creds.client_secret),
            headers=headers,
        )

    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Reddit OAuth token exchange failed: {_html_escape(str(resp.text))}</h2>", status_code=400
        )

    token_json = resp.json()
    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token", "")
    expires_in = token_json.get("expires_in")
    if not access_token:
        return HTMLResponse(
            "<h2>Reddit OAuth token response was missing an access token.</h2>", status_code=400
        )

    label = "Reddit Ads"
    async with httpx.AsyncClient(timeout=20) as client:
        me_resp = await client.get(
            "https://oauth.reddit.com/api/v1/me",
            headers={**headers, "Authorization": f"Bearer {access_token}"},
        )
    if me_resp.status_code == 200:
        label = me_resp.json().get("name") or label

    user_id = state_data["user_id"]
    project_id = state_data.get("project_id")
    encrypted_access = _encrypt(access_token)
    encrypted_refresh = _encrypt(refresh_token)
    expiry = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None

    async with app_state.db_session_factory() as db:
        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == uuid.UUID(user_id),
            OAuthConnection.provider == "reddit",
        )
        if project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == uuid.UUID(project_id))
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()

        if existing:
            existing.access_token_encrypted = encrypted_access
            existing.refresh_token_encrypted = encrypted_refresh
            existing.token_expiry = expiry
            existing.google_email = label
            existing.scopes = _SCOPES
            existing.connection_status = "active"
            existing.is_active = True
            if project_id:
                existing.project_id = uuid.UUID(project_id)
        else:
            db.add(
                OAuthConnection(
                    user_id=uuid.UUID(user_id),
                    project_id=uuid.UUID(project_id) if project_id else None,
                    provider="reddit",
                    google_email=label,
                    access_token_encrypted=encrypted_access,
                    refresh_token_encrypted=encrypted_refresh,
                    token_expiry=expiry,
                    scopes=_SCOPES,
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_id)))
    return RedirectResponse(url="/home?toast=reddit_connected", status_code=302)


@router.delete("/api/connections/reddit/{conn_id}")
async def disconnect_reddit_connection(conn_id: str, request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async with app_state.db_session_factory() as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == uuid.UUID(conn_id),
                OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
                OAuthConnection.provider == "reddit",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return JSONResponse({"success": True})
