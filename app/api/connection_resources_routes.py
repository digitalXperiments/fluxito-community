"""Linked resources of a Google connection (GA4 properties, GTM containers, Ads accounts).

GET  /api/connections/google/<conn_id>/resources — every discovered resource + whether it's linked
PUT  /api/connections/google/<conn_id>/resources — link / unlink one: {"type": "ga4"|"gtm"|"ads", "id": <row id>, "linked": bool}

Owner/admin of the connection's project only (the middleware connection gate
also covers the PUT). Unlinked resources drop out of the project context, so
AI tools in the project can't read or change them (``app.auth.resource_scope``).
New resources are discovered — and linked — when Google is reconnected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

import app.app_state as app_state
from app.auth.connection_gate import can_connect
from app.auth.uid_cookie import get_uid_from_request
from app.models.connection import OAuthConnection
from app.models.token import GA4Property, GoogleAdsAccount, GTMContainer

router = APIRouter()

_MODELS = {"ga4": GA4Property, "gtm": GTMContainer, "ads": GoogleAdsAccount}


async def _connection_for_admin(request: Request, conn_id: str) -> OAuthConnection:
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(401, "Not authenticated")
    try:
        cid = uuid.UUID(conn_id)
    except ValueError:
        raise HTTPException(404, "Connection not found")
    async with app_state.db_session_factory() as db:
        conn = await db.get(OAuthConnection, cid)
    if conn is None or conn.provider != "google" or not conn.is_active or conn.project_id is None:
        raise HTTPException(404, "Connection not found")
    if not await can_connect(uid, str(conn.project_id)):
        raise HTTPException(403, "Only project owners and admins can manage linked resources")
    return conn


def _row(kind: str, r) -> dict:
    if kind == "ga4":
        return {
            "id": str(r.id),
            "resource_id": r.property_id,
            "name": r.property_name or r.property_id,
            "group": r.account_name or r.account_id or "",
            "linked": bool(r.is_active),
        }
    if kind == "gtm":
        return {
            "id": str(r.id),
            "resource_id": r.public_id or r.container_id,
            "name": r.container_name or r.public_id or r.container_id,
            "group": r.account_id,
            "linked": bool(r.is_active),
        }
    return {
        "id": str(r.id),
        "resource_id": r.customer_id,
        "name": r.account_name or r.customer_id,
        "group": r.currency_code or "",
        "linked": bool(r.is_active),
    }


@router.get("/api/connections/google/{conn_id}/resources")
async def list_resources(request: Request, conn_id: str):
    conn = await _connection_for_admin(request, conn_id)
    out: dict[str, list[dict]] = {}
    async with app_state.db_session_factory() as db:
        for kind, model in _MODELS.items():
            rows = (await db.execute(select(model).where(model.connection_id == conn.id))).scalars().all()
            out[kind] = sorted((_row(kind, r) for r in rows), key=lambda d: (d["group"], d["name"]))
    return JSONResponse(out)


@router.put("/api/connections/google/{conn_id}/resources")
async def set_resource_link(request: Request, conn_id: str):
    conn = await _connection_for_admin(request, conn_id)
    body = await request.json()
    kind = str(body.get("type") or "")
    model = _MODELS.get(kind)
    if model is None:
        raise HTTPException(400, "type must be ga4, gtm or ads")
    try:
        row_id = uuid.UUID(str(body.get("id")))
    except ValueError:
        raise HTTPException(400, "Invalid resource id")
    async with app_state.db_session_factory() as db:
        row = (
            await db.execute(select(model).where(model.id == row_id, model.connection_id == conn.id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "Resource not found")
        row.is_active = bool(body.get("linked"))
        await db.commit()
        result = _row(kind, row)

    from app.auth.mcp_session_manager import invalidate_project_context_cache, invalidate_user_context_cache

    await invalidate_project_context_cache(str(conn.project_id))
    await invalidate_user_context_cache(str(conn.user_id))
    return JSONResponse(result)
