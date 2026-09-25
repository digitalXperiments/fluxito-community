"""X Ads OAuth 1.0a routes."""

from __future__ import annotations

from html import escape as _html_escape
import asyncio
import json
import secrets
import uuid
from urllib.parse import parse_qs, urlencode

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
from app.connectors.x_ads import XAdsConnector, XOAuth1Token
from app.models.connection import OAuthConnection

router = APIRouter(tags=["x-oauth"])


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


async def _x_connector(project_id: str | None = None) -> XAdsConnector:
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as db:
        creds = await get_oauth_app_credentials(db, "x", project_id=project_id)
    return XAdsConnector(creds.client_id, creds.client_secret)


@router.get("/connect/x", response_class=HTMLResponse)
async def connect_x_page(request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/x", status_code=302)

    return await _render_interstitial(
        request=request,
        platform_slug="x",
        platform_name="X Ads",
        platform_desc="Connect your X Ads account to read campaign, line item, analytics, and website tag data.",
        btn_bg="linear-gradient(135deg,#111111,#555555)",
        btn_text_color="#fff",
        btn_shadow="0 4px 20px rgba(0,0,0,0.25)",
        permissions=[
            (
                "Ads",
                "Ads API access",
                "Read advertising accounts, campaigns, line items, analytics, and conversion tag status.",
            ),
            (
                "Manage",
                "Campaign management",
                "Update campaign status when you ask Fluxito to make changes.",
            ),
        ],
        authorize_url="/connect/x/authorize",
        user_ctx=user_ctx,
    )


@router.get("/connect/x/authorize")
async def x_authorize(request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/x", status_code=302)

    from app.utils import base_url_from_request

    base_url = base_url_from_request(request)
    callback_url = f"{base_url}/auth/x/callback"
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    oauth_state = secrets.token_urlsafe(32)
    connector = await _x_connector(active_project_id)
    request_token_url = "https://api.x.com/oauth/request_token"
    header = connector._oauth_header(
        "POST",
        request_token_url,
        extra_oauth={"oauth_callback": callback_url},
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(request_token_url, headers={"Authorization": header})

    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>X OAuth request token failed: {_html_escape(str(resp.text))}</h2>", status_code=400
        )

    token_data = parse_qs(resp.text)
    oauth_token = token_data.get("oauth_token", [""])[0]
    oauth_token_secret = token_data.get("oauth_token_secret", [""])[0]
    if not oauth_token or not oauth_token_secret:
        return HTMLResponse(
            "<h2>X OAuth request token response was missing token data.</h2>", status_code=400
        )

    await app_state.redis_client.setex(
        f"x_oauth_request:{oauth_token}",
        600,
        json.dumps(
            {
                "state": oauth_state,
                "user_id": user_ctx.user_id,
                "project_id": active_project_id,
                "request_token": oauth_token,
                "request_token_secret": oauth_token_secret,
            }
        ),
    )
    authorize_url = "https://api.x.com/oauth/authorize?" + urlencode(
        {"oauth_token": oauth_token, "state": oauth_state}
    )
    return RedirectResponse(url=authorize_url)


@router.get("/auth/x/callback")
async def x_callback(
    request: Request,
    oauth_token: str = Query(default=None),
    oauth_verifier: str = Query(default=None),
    state: str | None = Query(default=None),
    denied: str | None = Query(default=None),
):
    if denied:
        return RedirectResponse("/home?error=x_denied", status_code=302)
    if not oauth_token or not oauth_verifier:
        return HTMLResponse("<h2>Invalid X callback - missing token or verifier.</h2>", status_code=400)

    raw = await app_state.redis_client.get(f"x_oauth_request:{oauth_token}")
    if not raw:
        return HTMLResponse("<h2>Invalid or expired X OAuth state.</h2>", status_code=400)
    await app_state.redis_client.delete(f"x_oauth_request:{oauth_token}")
    state_data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)

    if state_data.get("request_token") != oauth_token:
        return HTMLResponse("<h2>X OAuth token mismatch.</h2>", status_code=400)
    if state and state_data.get("state") != state:
        return HTMLResponse("<h2>X OAuth state mismatch.</h2>", status_code=400)

    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, state_data.get("user_id"), state_data.get("project_id"))
    if _denied is not None:
        return _denied

    connector = await _x_connector(state_data.get("project_id"))
    token = XOAuth1Token(oauth_token, state_data["request_token_secret"])
    access_token_url = "https://api.x.com/oauth/access_token"
    header = connector._oauth_header(
        "POST",
        access_token_url,
        token=token,
        extra_oauth={"oauth_verifier": oauth_verifier},
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(access_token_url, headers={"Authorization": header})

    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>X OAuth access token failed: {_html_escape(str(resp.text))}</h2>", status_code=400
        )

    token_data = parse_qs(resp.text)
    access_token = token_data.get("oauth_token", [""])[0]
    token_secret = token_data.get("oauth_token_secret", [""])[0]
    screen_name = token_data.get("screen_name", [""])[0]
    x_user_id = token_data.get("user_id", [""])[0]
    if not access_token or not token_secret:
        return HTMLResponse("<h2>X OAuth access token response was missing token data.</h2>", status_code=400)

    user_id = state_data["user_id"]
    project_id = state_data.get("project_id")
    encrypted_access = _encrypt(access_token)
    encrypted_secret = _encrypt(token_secret)

    async with app_state.db_session_factory() as db:
        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == uuid.UUID(user_id),
            OAuthConnection.provider == "x",
        )
        if project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == uuid.UUID(project_id))
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()

        label = f"@{screen_name}" if screen_name else f"x_{x_user_id}"
        if existing:
            existing.access_token_encrypted = encrypted_access
            existing.refresh_token_encrypted = encrypted_secret
            existing.google_email = label
            existing.scopes = ["oauth1a", "ads"]
            existing.connection_status = "active"
            existing.is_active = True
            if project_id:
                existing.project_id = uuid.UUID(project_id)
        else:
            db.add(
                OAuthConnection(
                    user_id=uuid.UUID(user_id),
                    project_id=uuid.UUID(project_id) if project_id else None,
                    provider="x",
                    google_email=label,
                    access_token_encrypted=encrypted_access,
                    refresh_token_encrypted=encrypted_secret,
                    scopes=["oauth1a", "ads"],
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_id)))
    return RedirectResponse(url="/home?toast=x_connected", status_code=302)


@router.delete("/api/connections/x/{conn_id}")
async def disconnect_x_connection(conn_id: str, request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async with app_state.db_session_factory() as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == uuid.UUID(conn_id),
                OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
                OAuthConnection.provider == "x",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()
    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}
