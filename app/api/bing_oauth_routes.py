"""Bing Webmaster Tools OAuth Routes (Microsoft Identity Platform).

Bing Webmaster Tools uses Microsoft account / Entra ID OAuth 2.0.
We use the consumer tenant for personal Microsoft accounts (most common for Bing Webmaster).
"""

import json
import secrets
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
import uuid

import app.app_state as app_state
from app.api.google_oauth_routes import _render_interstitial, _resolve_user_ctx, _encrypt
from app.config import settings
from app.models.connection import OAuthConnection

router = APIRouter(tags=["bing-oauth"])


# Microsoft Identity Platform endpoints for consumer accounts
# Using "consumers" tenant targets personal Microsoft accounts (most Bing Webmaster users).
_MS_AUTHORIZE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
_MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# The Bing Webmaster Tools API accepts Microsoft access tokens for authenticated calls.
# Scope below is the common pattern for apps calling Bing Webmaster on behalf of the user.
_BING_SCOPE = "https://ssl.bing.com/webmaster/api.svc/json/ offline_access"


@router.get("/connect/bing", response_class=HTMLResponse)
async def connect_bing(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/connect/bing", status_code=302)

    # Generate state
    state = secrets.token_urlsafe(32)
    state_data = {
        "user_id": user_ctx.user_id,
        # Validated by the request middleware (a project the user is a member of).
        "project_id": request.query_params.get("project_id") or request.cookies.get("active_project_id"),
        "nonce": secrets.token_hex(16),
    }
    await app_state.redis_client.setex(f"bing_oauth_state:{state}", 600, json.dumps(state_data))

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _bing_creds = await get_oauth_app_credentials(
            _cred_db, "bing", project_id=state_data.get("project_id")
        )

    params = {
        "response_type": "code",
        "client_id": _bing_creds.client_id,
        "redirect_uri": f"{settings.APP_BASE_URL}/auth/bing/callback",
        "scope": _BING_SCOPE,
        "state": state,
        "prompt": "consent",
    }
    authorize_url = _MS_AUTHORIZE + "?" + urlencode(params)

    permissions = [
        (
            "🔎",
            "Webmaster data",
            "Read your verified sites, crawl stats, query performance, and index coverage from Bing Webmaster Tools.",
        ),
        (
            "🔄",
            "Offline access",
            "Maintain access so scheduled reports and dashboards stay up to date without re-logging in.",
        ),
    ]

    return await _render_interstitial(
        request,
        "bing",
        "Bing Webmaster Tools",
        "Search performance, crawl stats, index coverage",
        "#008373",
        "#FFFFFF",
        "0 4px 14px 0 rgb(0 131 115 / 40%)",
        permissions,
        authorize_url,
        user_ctx,
    )


@router.get("/auth/bing/callback", response_class=RedirectResponse)
async def bing_callback(request: Request, code: str = Query(...), state: str = Query(...)):
    state_data_json = await app_state.redis_client.get(f"bing_oauth_state:{state}")
    if not state_data_json:
        return RedirectResponse("/home?error=invalid_state", status_code=302)

    state_data = json.loads(state_data_json)
    await app_state.redis_client.delete(f"bing_oauth_state:{state}")

    user_id = state_data["user_id"]
    project_id = state_data.get("project_id")

    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, state_data.get("user_id"), state_data.get("project_id"))
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _bing_creds = await get_oauth_app_credentials(_cred_db, "bing", project_id=project_id)

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{settings.APP_BASE_URL}/auth/bing/callback",
        "client_id": _bing_creds.client_id,
        "client_secret": _bing_creds.client_secret,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_MS_TOKEN, data=token_data)

    if resp.status_code != 200:
        return RedirectResponse("/home?error=token_exchange_failed", status_code=302)

    token_json = resp.json()
    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token", "")
    expires_in = token_json.get("expires_in")

    if not access_token:
        return RedirectResponse("/home?error=token_exchange_failed", status_code=302)

    expiry = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None

    encrypted_access = _encrypt(access_token)
    encrypted_refresh = _encrypt(refresh_token) if refresh_token else ""

    db_session = app_state.db_session_factory()
    async with db_session as db:
        conn = OAuthConnection(
            project_id=uuid.UUID(project_id) if project_id else None,
            user_id=uuid.UUID(user_id),
            provider="bing",
            google_email=None,
            access_token_encrypted=encrypted_access,
            refresh_token_encrypted=encrypted_refresh,
            token_expiry=expiry,
            scopes=[_BING_SCOPE],
            is_active=True,
            connection_status="active",
        )
        db.add(conn)
        await db.commit()

    return RedirectResponse("/home?connected=bing", status_code=302)


@router.delete("/api/connections/bing/{conn_id}")
async def disconnect_bing_connection(conn_id: str, request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        from fastapi.responses import JSONResponse as _JSONResponse

        return _JSONResponse({"error": "Unauthorized"}, status_code=401)

    from sqlalchemy import update as _update
    import asyncio as _asyncio
    from app.auth.mcp_session_manager import invalidate_user_context_cache

    async with app_state.db_session_factory() as db:
        await db.execute(
            _update(OAuthConnection)
            .where(
                OAuthConnection.id == uuid.UUID(conn_id),
                OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
                OAuthConnection.provider == "bing",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    _asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}
