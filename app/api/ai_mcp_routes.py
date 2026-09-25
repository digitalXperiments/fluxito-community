"""AI & MCP hub — connect AI clients, manage access, see what the AI may do.

One page (``/ai``) that gathers everything about using Fluxito from an AI
client: the MCP endpoint, per-client install instructions, the OAuth clients
the user has authorized (with revoke), personal access tokens, and the tool
domains the user's role grants in the active project.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

import app.app_state as app_state
from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx
from app.api.project_routes import ensure_active_project, set_active_project_cookie
from app.auth.mcp_session_manager import list_oauth_clients, list_pats, revoke_oauth_client
from app.auth.permissions import resolve_effective_permissions
from app.models.audit import ToolCallAudit
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-mcp"])

# Tool domains shown on "What your AI can do". Keys match app.auth.permissions
# DOMAIN_TOOLS; ``read_only`` domains have no write level.
AI_DOMAINS: list[dict] = [
    {
        "key": "analytics",
        "label": "Analytics",
        "covers": "GA4, Adobe Analytics, Amplitude, Mixpanel, PostHog",
    },
    {"key": "tagmanager", "label": "Tag manager", "covers": "GTM containers, workspaces and drafts"},
    {
        "key": "marketing",
        "label": "Ad platforms",
        "covers": "Campaigns, spend and budgets across 9 ad platforms",
    },
    {"key": "audience", "label": "Audiences", "covers": "Audience lists and segments"},
    {"key": "seo", "label": "Search", "covers": "Search Console and Bing Webmaster Tools"},
    {"key": "warehouse", "label": "Warehouse", "covers": "BigQuery, Snowflake, Redshift", "read_only": True},
    {"key": "knowledge", "label": "Knowledge", "covers": "KPI library and business context"},
    {
        "key": "analysis",
        "label": "Analysis",
        "covers": "Audits, rule books, live tag tests and cross-platform reports",
    },
]


def _domain_access(eff) -> list[dict]:
    rows = []
    for d in AI_DOMAINS:
        levels = set() if eff is None else eff.tools.get(d["key"], set())
        full = bool(eff and eff.full)
        rows.append(
            {
                **d,
                "read": full or "read" in levels or "write" in levels,
                "write": (full or "write" in levels) and not d.get("read_only"),
            }
        )
    return rows


@router.get("/ai")
async def ai_mcp_page(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/ai", status_code=302)
    pid_str = await ensure_active_project(request, user_ctx.user_id)
    user_view = await _load_user_view(user_ctx)

    clients: list[dict] = []
    tokens: list[dict] = []
    calls_24h = 0
    eff = None
    try:
        clients = await list_oauth_clients(user_ctx.user_id)
        tokens = await list_pats(user_ctx.user_id)
    except Exception as e:  # pragma: no cover - defensive; page still renders
        logger.warning(f"AI & MCP: could not load clients/tokens: {e}")
    try:
        async with app_state.db_session_factory() as db:
            since = datetime.utcnow() - timedelta(hours=24)
            calls_24h = (
                await db.execute(
                    select(func.count())
                    .select_from(ToolCallAudit)
                    .where(
                        ToolCallAudit.user_id == uuid.UUID(str(user_ctx.user_id)),
                        ToolCallAudit.created_at >= since,
                    )
                )
            ).scalar_one() or 0
    except Exception as e:  # pragma: no cover
        logger.warning(f"AI & MCP: could not count tool calls: {e}")
    if pid_str:
        try:
            eff = await resolve_effective_permissions(str(user_ctx.user_id), pid_str)
        except Exception as e:  # pragma: no cover
            logger.warning(f"AI & MCP: could not resolve permissions: {e}")

    response = render(
        request,
        "ai_mcp.html",
        {
            "user": user_view,
            "active": "ai_mcp",
            "oauth_clients": clients,
            "pat_tokens": tokens,
            "tool_calls_24h": calls_24h,
            "domains": _domain_access(eff),
            "full_access": bool(eff and eff.full),
        },
    )
    if pid_str:
        set_active_project_cookie(response, pid_str)
    return response


@router.get("/api/ai/clients")
async def api_list_ai_clients(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"clients": await list_oauth_clients(user_ctx.user_id)})


@router.post("/api/ai/clients/{client_id}/revoke")
async def api_revoke_ai_client(request: Request, client_id: str):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    revoked = await revoke_oauth_client(user_ctx.user_id, client_id)
    if not revoked:
        return JSONResponse({"error": "No active authorization for that client"}, status_code=404)
    try:
        from app.notifications import create_notification

        await create_notification(
            user_id=user_ctx.user_id,
            title="AI client access revoked",
            message="An AI client can no longer use Fluxito on your behalf until it signs in again.",
            category="system",
            severity="warning",
            action_url="/ai#connected",
        )
    except Exception:
        pass
    return JSONResponse({"ok": True, "revoked_sessions": revoked})
