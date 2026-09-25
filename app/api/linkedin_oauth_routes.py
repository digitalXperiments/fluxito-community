"""LinkedIn Ads OAuth Routes"""

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

router = APIRouter(tags=["linkedin-oauth"])


@router.get("/connect/linkedin", response_class=HTMLResponse)
async def connect_linkedin(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/connect/linkedin", status_code=302)

    # Generate state
    state = secrets.token_urlsafe(32)
    state_data = {
        "user_id": user_ctx.user_id,
        # Validated by the request middleware (a project the user is a member of).
        "project_id": request.query_params.get("project_id") or request.cookies.get("active_project_id"),
        "nonce": secrets.token_hex(16),
    }
    await app_state.redis_client.setex(f"linkedin_oauth_state:{state}", 600, json.dumps(state_data))

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _linkedin_creds = await get_oauth_app_credentials(
            _cred_db, "linkedin", project_id=state_data.get("project_id")
        )

    params = {
        "response_type": "code",
        "client_id": _linkedin_creds.client_id,
        "redirect_uri": f"{settings.APP_BASE_URL}/api/connections/linkedin/callback",
        "scope": "r_ads r_ads_reporting",
        "state": state,
    }
    authorize_url = "https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params)

    permissions = [
        (
            "📣",
            "r_ads",
            "Read your ad accounts, campaigns, and ad groups — including budget, status, and targeting.",
        ),
        (
            "📊",
            "r_ads_reporting",
            "Fetch performance data — impressions, clicks, spend, and conversion metrics.",
        ),
    ]

    return await _render_interstitial(
        request,
        "linkedin",
        "LinkedIn Ads",
        "Campaigns, ad groups, Insight Tag audits",
        "#0077B5",
        "#FFFFFF",
        "0 4px 14px 0 rgb(0 119 181 / 40%)",
        permissions,
        authorize_url,
        user_ctx,
    )


@router.get("/api/connections/linkedin/callback", response_class=RedirectResponse)
async def linkedin_callback(request: Request, code: str = Query(...), state: str = Query(...)):
    # Validate state
    state_data_json = await app_state.redis_client.get(f"linkedin_oauth_state:{state}")
    if not state_data_json:
        return RedirectResponse("/home?error=invalid_state", status_code=302)

    state_data = json.loads(state_data_json)
    await app_state.redis_client.delete(f"linkedin_oauth_state:{state}")

    user_id = state_data["user_id"]
    project_id = state_data.get("project_id")

    # Exchange code for token
    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, state_data.get("user_id"), state_data.get("project_id"))
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _linkedin_creds = await get_oauth_app_credentials(_cred_db, "linkedin", project_id=project_id)

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{settings.APP_BASE_URL}/api/connections/linkedin/callback",
        "client_id": _linkedin_creds.client_id,
        "client_secret": _linkedin_creds.client_secret,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://www.linkedin.com/oauth/v2/accessToken", data=token_data)

    if resp.status_code != 200:
        return RedirectResponse("/home?error=token_exchange_failed", status_code=302)

    token_json = resp.json()
    access_token = token_json["access_token"]
    refresh_token = token_json.get("refresh_token", "")
    expires_in = token_json.get("expires_in")

    expiry = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None

    encrypted_access = _encrypt(access_token)
    encrypted_refresh = _encrypt(refresh_token)

    # Save to DB
    db_session = app_state.db_session_factory()
    async with db_session as db:
        conn = OAuthConnection(
            project_id=uuid.UUID(project_id) if project_id else None,
            user_id=uuid.UUID(user_id),
            provider="linkedin",
            access_token_encrypted=encrypted_access,
            refresh_token_encrypted=encrypted_refresh,
            token_expiry=expiry,
            scopes=["r_ads", "r_ads_reporting"],
            is_active=True,
            connection_status="active",
        )
        db.add(conn)
        await db.commit()

    return RedirectResponse("/home?connected=linkedin", status_code=302)
