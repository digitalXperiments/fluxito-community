"""
Knowledge Base Routes — KPI Library + Business Context.

Both features are scoped to a **project**. All KPIs and business context
documents belong to a project, identified by the ``active_project_id``
cookie set from the project management UI.

The KPI library moved from a freeform glossary to a structured catalog
(migration 030). A KPI now has identity (slug, aliases, status, version),
a definition, a computation spec (expression + bound inputs), and
quality metadata (unit, direction, targets). See
``app/models/knowledge.py`` for the full shape.

Routes
------
HTML pages (signed uid cookie auth):
  GET    /kpi-library                — List / manage KPIs
  GET    /business-context           — Edit the business context .md doc

JSON API (same auth, used by the pages):
  GET    /api/kpi-library            — List all KPIs for the active project
  POST   /api/kpi-library            — Create a KPI
  PUT    /api/kpi-library/{kpi_id}   — Update a KPI
  DELETE /api/kpi-library/{kpi_id}   — Delete a KPI
  GET    /api/business-context       — Fetch the context document
  PUT    /api/business-context       — Replace the context document
"""

import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

import app.app_state as app_state
from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx
from app.api.project_routes import (
    ensure_active_project,
    set_active_project_cookie,
)
from app.models.knowledge import KPI, BusinessContext, KPIInput
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()


_STATUSES = {"draft", "approved", "deprecated"}
_DIRECTIONS = {"higher_better", "lower_better", "neutral"}


def _slugify(value: str) -> str:
    """Turn a KPI name into a stable, URL-safe slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return s[:128] or "kpi"


# ---------------------------------------------------------------------------
# Project-scoped auth helper
# ---------------------------------------------------------------------------


async def _require_user_and_project(request: Request, level: str = "read"):
    """Resolve auth + active project + ``knowledge`` permission at ``level``.

    Returns ``(user_ctx, user_uuid, project_id)``. With RBAC on, a member needs
    their role to grant ``knowledge`` read (GETs, compute) or write (edits).
    """
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        user_uuid = uuid.UUID(user_ctx.user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized")

    project_id_str = await ensure_active_project(request, user_ctx.user_id)
    if not project_id_str:
        raise HTTPException(
            status_code=400,
            detail="No active project. Create or select a project first.",
        )
    project_id = uuid.UUID(project_id_str)
    from app.auth.web_guards import require_domain_permission

    await require_domain_permission(user_ctx.user_id, project_id_str, "knowledge", level)
    return user_ctx, user_uuid, project_id


# ---------------------------------------------------------------------------
# Pydantic payloads
# ---------------------------------------------------------------------------


_SUPPORTED_SOURCES = {
    "ga4",
    "bigquery",
    "google_ads",
    "search_console",
    "amplitude",
    "mixpanel",
    "posthog",
    "adobe_analytics",
    "meta_ads",
    "tiktok_ads",
    "snap_ads",
}


class KPIInputPayload(BaseModel):
    """One bound input for a KPI expression."""

    key: str = Field(..., min_length=1, max_length=32)
    source: str = Field(..., min_length=1, max_length=32)
    connection_id: str
    binding: dict[str, Any] = Field(default_factory=dict)


class KPIPayload(BaseModel):
    # Identity
    name: str = Field(..., min_length=1, max_length=255)
    slug: str | None = Field(None, max_length=128)
    aliases: list[str] = Field(default_factory=list)

    # Lifecycle
    status: str = Field("draft")
    version: int = Field(1, ge=1)

    # Definition
    description: str = Field(..., min_length=1)
    business_question: str | None = None
    interpretation_guide: str | None = None
    category: str | None = Field(None, max_length=64)
    tags: list[str] = Field(default_factory=list)

    # Computation
    expression: str | None = None
    time_grain: str | None = Field(None, max_length=32)
    unit: str | None = Field(None, max_length=32)
    format_spec: str | None = Field(None, max_length=64)
    direction: str | None = Field(None, max_length=16)
    inputs: list[KPIInputPayload] = Field(default_factory=list)

    # Quality
    target_value: float | None = None
    target_type: str | None = Field(None, max_length=32)
    expected_range_min: float | None = None
    expected_range_max: float | None = None

    # Ownership
    owner: str | None = Field(None, max_length=255)
    source_of_truth_url: str | None = Field(None, max_length=512)


class BusinessContextPayload(BaseModel):
    content: str = ""


def _validate_status_direction(payload: KPIPayload) -> None:
    if payload.status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{payload.status}'")
    if payload.direction is not None and payload.direction not in _DIRECTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid direction '{payload.direction}'")


def _apply_payload(kpi: KPI, payload: KPIPayload) -> None:
    """Copy scalar fields from payload onto the KPI instance."""
    kpi.name = payload.name.strip()
    kpi.slug = payload.slug.strip() if payload.slug else _slugify(payload.name)
    kpi.aliases = [a.strip() for a in payload.aliases if a and a.strip()]
    kpi.status = payload.status
    kpi.version = payload.version
    kpi.description = payload.description.strip()
    kpi.business_question = (payload.business_question or "").strip() or None
    kpi.interpretation_guide = (payload.interpretation_guide or "").strip() or None
    kpi.category = (payload.category or "").strip() or None
    kpi.tags = [t.strip() for t in payload.tags if t and t.strip()]
    kpi.expression = (payload.expression or "").strip() or None
    kpi.time_grain = (payload.time_grain or "").strip() or None
    kpi.unit = (payload.unit or "").strip() or None
    kpi.format_spec = (payload.format_spec or "").strip() or None
    kpi.direction = payload.direction
    kpi.target_value = payload.target_value
    kpi.target_type = (payload.target_type or "").strip() or None
    kpi.expected_range_min = payload.expected_range_min
    kpi.expected_range_max = payload.expected_range_max
    kpi.owner = (payload.owner or "").strip() or None
    kpi.source_of_truth_url = (payload.source_of_truth_url or "").strip() or None


async def _assert_inputs_in_project(inputs: list[KPIInput], project_id: uuid.UUID) -> None:
    """Every KPI input must read from a connection of *this* project."""
    from app.auth.connection_gate import project_of_connection

    for inp in inputs:
        found, owner_pid = await project_of_connection(str(inp.connection_id))
        if not found or owner_pid != str(project_id):
            raise HTTPException(
                status_code=400,
                detail=f"Input '{inp.key}' must use a connection from this project.",
            )


def _build_inputs(payload: KPIPayload) -> list[KPIInput]:
    rows: list[KPIInput] = []
    seen: set[str] = set()
    for inp in payload.inputs:
        key = inp.key.strip()
        if not key:
            continue
        if key in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate input key '{key}'")
        seen.add(key)
        source = inp.source.strip().lower()
        if source not in _SUPPORTED_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported source '{source}' for input '{key}'",
            )
        try:
            conn_uuid = uuid.UUID(inp.connection_id)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid connection_id for input '{key}'")
        rows.append(
            KPIInput(
                key=key,
                source=source,
                connection_id=conn_uuid,
                binding=dict(inp.binding or {}),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Context hub — /context (merges KPI Library + Business Context)
# ---------------------------------------------------------------------------


@router.get("/context")
async def context_page(request: Request):
    """Redirect to the default sub-page of the context section."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/context", status_code=302)

    project_id_str = await ensure_active_project(request, user_ctx.user_id)
    if not project_id_str:
        return RedirectResponse("/projects", status_code=302)

    return RedirectResponse("/kpi-library", status_code=302)


# ---------------------------------------------------------------------------
# KPI Library — HTML page
# ---------------------------------------------------------------------------


@router.get("/kpi-library")
async def kpi_library_page(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/kpi-library", status_code=302)

    project_id_str = await ensure_active_project(request, user_ctx.user_id)
    if not project_id_str:
        return RedirectResponse("/projects", status_code=302)

    project_id = uuid.UUID(project_id_str)

    from app.auth.web_guards import require_domain_permission

    await require_domain_permission(user_ctx.user_id, project_id_str, "knowledge")

    user_view = await _load_user_view(user_ctx, project_id=project_id_str)

    async with app_state.db_session_factory() as db:
        stmt = (
            select(KPI)
            .where(KPI.project_id == project_id)
            .options(selectinload(KPI.inputs))
            .order_by(KPI.category.nullsfirst(), KPI.name)
        )
        result = await db.execute(stmt)
        kpis = [k.to_dict() for k in result.scalars().all()]

    response = render(
        request,
        "kpi_library.html",
        {
            "user": user_view,
            "kpis": kpis,
            "scope_type": "project",
        },
    )
    set_active_project_cookie(response, project_id_str)
    return response


# ---------------------------------------------------------------------------
# KPI Library — JSON API
# ---------------------------------------------------------------------------


@router.get("/api/kpi-library")
async def list_kpis(request: Request):
    _, _, project_id = await _require_user_and_project(request)
    async with app_state.db_session_factory() as db:
        stmt = (
            select(KPI)
            .where(KPI.project_id == project_id)
            .options(selectinload(KPI.inputs))
            .order_by(KPI.category.nullsfirst(), KPI.name)
        )
        result = await db.execute(stmt)
        return JSONResponse({"kpis": [k.to_dict() for k in result.scalars().all()]})


@router.post("/api/kpi-library")
async def create_kpi(payload: KPIPayload, request: Request):
    _, user_uuid, project_id = await _require_user_and_project(request, "write")
    _validate_status_direction(payload)

    kpi = KPI(project_id=project_id, name="", slug="", description="", created_by=user_uuid)
    _apply_payload(kpi, payload)
    kpi.inputs = _build_inputs(payload)
    await _assert_inputs_in_project(kpi.inputs, project_id)

    async with app_state.db_session_factory() as db:
        db.add(kpi)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            msg = str(e).lower()
            if "uq_kpis_project_slug" in msg or "uq_kpis_project_name" in msg or "unique" in msg:
                raise HTTPException(
                    status_code=409,
                    detail="A KPI with that name or slug already exists.",
                )
            logger.exception("Failed to create KPI")
            raise HTTPException(status_code=500, detail="Failed to create KPI")
        await db.refresh(kpi, attribute_names=["inputs"])
        return JSONResponse({"kpi": kpi.to_dict()}, status_code=201)


@router.put("/api/kpi-library/{kpi_id}")
async def update_kpi(kpi_id: str, payload: KPIPayload, request: Request):
    _, _, project_id = await _require_user_and_project(request, "write")
    _validate_status_direction(payload)
    try:
        kpi_uuid = uuid.UUID(kpi_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid KPI id")

    async with app_state.db_session_factory() as db:
        stmt = select(KPI).where(KPI.id == kpi_uuid).options(selectinload(KPI.inputs))
        result = await db.execute(stmt)
        kpi = result.scalar_one_or_none()
        if not kpi or kpi.project_id != project_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        _apply_payload(kpi, payload)
        new_inputs = _build_inputs(payload)
        await _assert_inputs_in_project(new_inputs, project_id)
        # Replace the inputs wholesale via raw SQL + direct add rather than
        # reassigning the ORM collection. Reassigning fires orphan-delete
        # cascade + back_populates remove events that can trip greenlet
        # errors in async mode and also risk colliding with
        # ``uq_kpi_inputs_kpi_key`` when input keys are retained.
        await db.execute(delete(KPIInput).where(KPIInput.kpi_id == kpi.id))
        await db.flush()
        db.expire(kpi, ["inputs"])
        for new_input in new_inputs:
            new_input.kpi_id = kpi.id
            db.add(new_input)

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            msg = str(e).lower()
            if "uq_kpis_project_slug" in msg or "uq_kpis_project_name" in msg:
                raise HTTPException(
                    status_code=409,
                    detail="A KPI with that name or slug already exists.",
                )
            if "uq_kpi_inputs_kpi_key" in msg:
                raise HTTPException(
                    status_code=400,
                    detail="Duplicate input key — each input key must be unique within a KPI.",
                )
            logger.exception("Failed to update KPI")
            raise HTTPException(status_code=500, detail="Failed to update KPI")
        # Re-query after commit so all scalar attrs and inputs are loaded fresh,
        # avoiding expired-attribute lazy loads that trip the async greenlet.
        result2 = await db.execute(select(KPI).where(KPI.id == kpi_uuid).options(selectinload(KPI.inputs)))
        kpi = result2.scalar_one()
        return JSONResponse({"kpi": kpi.to_dict()})


class KPIComputePayload(BaseModel):
    date_range_start: str | None = None
    date_range_end: str | None = None


@router.post("/api/kpi-library/{kpi_id}/compute")
async def compute_kpi_route(kpi_id: str, payload: KPIComputePayload, request: Request):
    """Preview a KPI's computed value for the given time range."""
    from app.services.kpi_executor import compute_kpi_by_id

    _, _, project_id = await _require_user_and_project(request)
    try:
        kpi_uuid = uuid.UUID(kpi_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid KPI id")

    result = await compute_kpi_by_id(
        kpi_uuid,
        project_id,
        payload.date_range_start,
        payload.date_range_end,
    )
    return JSONResponse(result)


@router.delete("/api/kpi-library/{kpi_id}")
async def delete_kpi(kpi_id: str, request: Request):
    _, _, project_id = await _require_user_and_project(request, "write")
    try:
        kpi_uuid = uuid.UUID(kpi_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid KPI id")

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(KPI).where(KPI.id == kpi_uuid))
        kpi = result.scalar_one_or_none()
        if not kpi or kpi.project_id != project_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        await db.execute(delete(KPI).where(KPI.id == kpi_uuid))
        await db.commit()
        return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Business Context — HTML page
# ---------------------------------------------------------------------------


@router.get("/business-context")
async def business_context_page(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/business-context", status_code=302)

    project_id_str = await ensure_active_project(request, user_ctx.user_id)
    if not project_id_str:
        return RedirectResponse("/projects", status_code=302)

    from app.auth.web_guards import require_domain_permission

    await require_domain_permission(user_ctx.user_id, project_id_str, "knowledge")
    user_view = await _load_user_view(user_ctx)
    project_id = uuid.UUID(project_id_str)

    content = await _load_business_context_content(project_id)
    # KPI count for the "KPI library · N" tab on this page.
    async with app_state.db_session_factory() as db:
        kpi_count = (
            await db.execute(select(func.count()).select_from(KPI).where(KPI.project_id == project_id))
        ).scalar_one()

    response = render(
        request,
        "business_context.html",
        {
            "user": user_view,
            "content": content,
            "kpi_count": kpi_count,
            "scope_type": "project",
        },
    )
    set_active_project_cookie(response, project_id_str)
    return response


async def _load_business_context_content(project_id: uuid.UUID) -> str:
    async with app_state.db_session_factory() as db:
        stmt = select(BusinessContext).where(BusinessContext.project_id == project_id)
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()
    return doc.content if doc else ""


# ---------------------------------------------------------------------------
# Business Context — JSON API
# ---------------------------------------------------------------------------


@router.get("/api/business-context")
async def get_business_context(request: Request):
    _, _, project_id = await _require_user_and_project(request)
    content = await _load_business_context_content(project_id)
    return JSONResponse({"content": content})


@router.put("/api/business-context")
async def put_business_context(payload: BusinessContextPayload, request: Request):
    _, user_uuid, project_id = await _require_user_and_project(request, "write")

    async with app_state.db_session_factory() as db:
        stmt = select(BusinessContext).where(BusinessContext.project_id == project_id)
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()

        if doc is None:
            doc = BusinessContext(
                project_id=project_id,
                content=payload.content or "",
                updated_by=user_uuid,
            )
            db.add(doc)
        else:
            doc.content = payload.content or ""
            doc.updated_by = user_uuid

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to save business context")
            raise HTTPException(status_code=500, detail="Failed to save business context")
        return JSONResponse({"ok": True})
