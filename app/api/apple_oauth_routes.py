"""Apple Ads OAuth client-credentials routes."""

from __future__ import annotations

from html import escape as _html_escape
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
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
from app.connectors.apple_ads import AppleAdsConnector
from app.models.connection import OAuthConnection

router = APIRouter(tags=["apple-oauth"])


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


async def _apple_creds(project_id: str | None = None):
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as db:
        return await get_oauth_app_credentials(db, "apple", project_id=project_id)


@router.get("/connect/apple", response_class=HTMLResponse)
async def connect_apple_page(request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/apple", status_code=302)

    return await _render_interstitial(
        request=request,
        platform_slug="apple",
        platform_name="Apple Ads",
        platform_desc="Connect Apple Ads to read organization, campaign, ad group, and reporting data.",
        btn_bg="linear-gradient(135deg,#111111,#6b7280)",
        btn_text_color="#fff",
        btn_shadow="0 4px 20px rgba(17,24,39,0.25)",
        permissions=[
            (
                "Campaigns",
                "searchadsorg",
                "Read Apple Ads organizations, campaigns, ad groups, and performance reports.",
            ),
            (
                "Manage",
                "Campaign management",
                "Update campaign status when you ask Fluxito to make changes.",
            ),
        ],
        authorize_url="/connect/apple/authorize",
        user_ctx=user_ctx,
    )


@router.get("/connect/apple/authorize")
async def apple_authorize(request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/apple", status_code=302)

    project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    creds = await _apple_creds(project_id)
    connector = AppleAdsConnector()
    token_json = await connector.request_access_token(creds.client_id, creds.client_secret)
    if token_json.get("error"):
        return HTMLResponse(f"<h2>{_html_escape(str(token_json['message']))}</h2>", status_code=400)

    access_token = token_json.get("access_token")
    expires_in = token_json.get("expires_in")
    if not access_token:
        return HTMLResponse(
            "<h2>Apple Ads OAuth token response was missing an access token.</h2>",
            status_code=400,
        )

    accounts_result = await connector.list_accounts(access_token)
    if accounts_result.get("error"):
        return HTMLResponse(f"<h2>{_html_escape(str(accounts_result['message']))}</h2>", status_code=400)

    accounts = accounts_result.get("accounts") or []
    first_account = accounts[0] if accounts else {}
    label = first_account.get("name") or "Apple Ads"
    encrypted_access = _encrypt(access_token)
    expiry = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None

    async with app_state.db_session_factory() as db:
        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
            OAuthConnection.provider == "apple",
        )
        if project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == uuid.UUID(project_id))
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()

        scopes = ["client_credentials", "searchadsorg"]
        if existing:
            existing.access_token_encrypted = encrypted_access
            existing.refresh_token_encrypted = ""
            existing.token_expiry = expiry
            existing.google_email = label
            existing.scopes = scopes
            existing.connection_status = "active"
            existing.is_active = True
            if project_id:
                existing.project_id = uuid.UUID(project_id)
        else:
            db.add(
                OAuthConnection(
                    user_id=uuid.UUID(user_ctx.user_id),
                    project_id=uuid.UUID(project_id) if project_id else None,
                    provider="apple",
                    google_email=label,
                    access_token_encrypted=encrypted_access,
                    refresh_token_encrypted="",
                    token_expiry=expiry,
                    scopes=scopes,
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return RedirectResponse(url="/home?toast=apple_connected", status_code=302)


@router.delete("/api/connections/apple/{conn_id}")
async def disconnect_apple_connection(conn_id: str, request: Request):
    user_ctx = await _load_user_ctx(request)
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    async with app_state.db_session_factory() as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == uuid.UUID(conn_id),
                OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
                OAuthConnection.provider == "apple",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return JSONResponse({"success": True})
