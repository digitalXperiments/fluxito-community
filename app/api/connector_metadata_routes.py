"""
Connector Metadata Routes — feeds the KPI library's field picker.

The KPI library's structured formula builder needs to enumerate every
dimension / metric / table / column available across the project's
connected sources. This module exposes per-source metadata endpoints,
backed by a short Redis cache so the picker feels instant after the
first fetch.

Scope: project-scoped. A user can only browse metadata for connections
that belong to their active project.

Routes
------
GET /api/kpi-library/sources
    List every source available to the active project (GA4 properties
    per OAuth connection + BigQuery projects). Used to populate the
    top-level source selector in the picker.

GET /api/kpi-library/metadata/ga4/fields
    ?connection_id=…&property_id=…&refresh=0
    Returns ``{dimensions, metrics}`` for a GA4 property.

GET /api/kpi-library/metadata/bigquery/datasets
    ?connection_id=…&refresh=0
    Returns the list of datasets in a BigQuery connection.

GET /api/kpi-library/metadata/bigquery/tables
    ?connection_id=…&dataset=…&refresh=0
    Returns the tables in a BigQuery dataset.

GET /api/kpi-library/metadata/bigquery/columns
    ?connection_id=…&dataset=…&table=…&refresh=0
    Returns the schema (columns + types) of a BigQuery table.

Pass ``refresh=1`` to force a live re-fetch and rewrite the cache.

Cache TTLs
----------
GA4 metadata: 24 hours (changes rarely).
BigQuery schema: 1 hour (tables are mutable).
"""

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

import app.app_state as app_state
from app.api.knowledge_routes import _require_user_and_project
from app.models.bq_connection import BQConnection
from app.models.connection import OAuthConnection
from app.models.token import GA4Property

logger = logging.getLogger(__name__)

router = APIRouter()


# 24h for GA4 (catalog changes rarely), 1h for BigQuery schemas (mutable).
_TTL_GA4 = 24 * 60 * 60
_TTL_BQ = 60 * 60


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------


async def _cache_get(key: str) -> Any | None:
    redis = app_state.redis_client
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception:
        logger.exception("Redis get failed for %s", key)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return None


async def _cache_set(key: str, value: Any, ttl: int) -> None:
    redis = app_state.redis_client
    if redis is None:
        return
    try:
        await redis.setex(key, ttl, json.dumps(value))
    except Exception:
        logger.exception("Redis setex failed for %s", key)


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------


async def _resolve_oauth_connection(conn_id: uuid.UUID, project_id: uuid.UUID) -> OAuthConnection:
    """Fetch a project-scoped OAuth connection or 404."""
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(OAuthConnection).where(OAuthConnection.id == conn_id))
        conn = result.scalar_one_or_none()
    if not conn or conn.project_id != project_id or not conn.is_active:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


async def _resolve_bq_connection(conn_id: uuid.UUID, project_id: uuid.UUID) -> BQConnection:
    """Fetch a project-scoped BigQuery connection or 404."""
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(BQConnection).where(BQConnection.id == conn_id))
        conn = result.scalar_one_or_none()
    if not conn or conn.fluxito_project_id != project_id or not conn.is_active:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


# ---------------------------------------------------------------------------
# /api/kpi-library/sources
# ---------------------------------------------------------------------------


@router.get("/api/kpi-library/sources")
async def list_sources(request: Request):
    """
    Returns every source the KPI picker can bind against for the active
    project — one entry per connection, with just enough info to render
    the top-level source selector (no per-property metadata fetched).
    """
    _, _, project_id = await _require_user_and_project(request)

    sources: list[dict[str, Any]] = []

    async with app_state.db_session_factory() as db:
        # Google OAuth connections — one connection can host multiple
        # platforms (GA4, GTM, Ads, GSC) via its scopes. Expose only the
        # ones we actually support in the picker today.
        oauth_rows = (
            (
                await db.execute(
                    select(OAuthConnection)
                    .where(OAuthConnection.project_id == project_id)
                    .where(OAuthConnection.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )

        # Pre-load GA4 properties for all Google connections in one query.
        oauth_ids = [c.id for c in oauth_rows if c.provider == "google"]
        properties_by_conn: dict[uuid.UUID, list[GA4Property]] = {}
        if oauth_ids:
            prop_rows = (
                (await db.execute(select(GA4Property).where(GA4Property.connection_id.in_(oauth_ids))))
                .scalars()
                .all()
            )
            for p in prop_rows:
                properties_by_conn.setdefault(p.connection_id, []).append(p)

        for c in oauth_rows:
            if c.provider != "google":
                continue
            scopes = set(c.scopes or [])
            has_ga4 = any("analytics" in s for s in scopes)
            if has_ga4:
                sources.append(
                    {
                        "source": "ga4",
                        "connection_id": str(c.id),
                        "label": c.google_email or "GA4",
                        "properties": [
                            {
                                # Strip the "properties/" prefix so the UI
                                # can use the bare numeric id in forms.
                                "id": (p.property_id or "").removeprefix("properties/"),
                                "display_name": p.property_name or p.property_id or "",
                            }
                            for p in properties_by_conn.get(c.id, [])
                        ],
                    }
                )

        # BigQuery connections — each row is one GCP project.
        bq_rows = (
            (
                await db.execute(
                    select(BQConnection)
                    .where(BQConnection.fluxito_project_id == project_id)
                    .where(BQConnection.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        for b in bq_rows:
            sources.append(
                {
                    "source": "bigquery",
                    "connection_id": str(b.id),
                    "label": b.display_name or b.project_id,
                    "project_id": b.project_id,
                    "datasets_hint": list(b.datasets or []),
                }
            )

    return JSONResponse({"sources": sources})


# ---------------------------------------------------------------------------
# /api/kpi-library/metadata/ga4/fields
# ---------------------------------------------------------------------------


@router.get("/api/kpi-library/metadata/ga4/properties")
async def ga4_properties(
    request: Request,
    connection_id: str = Query(...),
    refresh: int = Query(0),
):
    """
    Live-fetch GA4 properties for a connection via the Admin API.

    The ``/sources`` endpoint reads the ``ga4_properties`` table (populated
    during OAuth callback). This route bypasses that cache and queries the
    Admin API directly — used by the KPI picker when the cached list is
    empty or stale, so users don't need to re-run the OAuth flow to see
    their properties.
    """
    _, _, project_id = await _require_user_and_project(request)
    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connection_id")

    await _resolve_oauth_connection(conn_uuid, project_id)

    cache_key = f"kpi:meta:ga4:properties:{connection_id}"
    if not refresh:
        cached = await _cache_get(cache_key)
        if cached is not None:
            return JSONResponse({"cached": True, **cached})

    if app_state.ga4_connector is None or app_state.token_manager is None:
        raise HTTPException(status_code=503, detail="GA4 connector unavailable")

    token = await app_state.token_manager.get_valid_access_token(connection_id)
    raw = await app_state.ga4_connector.list_all_properties_raw(token)
    properties = [
        {
            # Strip "properties/" prefix for the UI; ``id`` is the bare number.
            "id": (p.get("id") or "").removeprefix("properties/"),
            "display_name": p.get("displayName") or p.get("name") or p.get("id") or "",
            "account": p.get("account"),
            "account_name": p.get("accountName"),
        }
        for p in raw
    ]
    result = {"properties": properties}
    await _cache_set(cache_key, result, _TTL_GA4)
    return JSONResponse({"cached": False, **result})


@router.get("/api/kpi-library/metadata/ga4/fields")
async def ga4_fields(
    request: Request,
    connection_id: str = Query(...),
    property_id: str = Query(...),
    refresh: int = Query(0),
):
    _, _, project_id = await _require_user_and_project(request)
    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connection_id")

    await _resolve_oauth_connection(conn_uuid, project_id)

    cache_key = f"kpi:meta:ga4:{connection_id}:{property_id}"
    if not refresh:
        cached = await _cache_get(cache_key)
        if cached is not None:
            return JSONResponse({"cached": True, **cached})

    if app_state.ga4_connector is None:
        raise HTTPException(status_code=503, detail="GA4 connector unavailable")

    result = await app_state.ga4_connector.get_metadata(connection_id, property_id)
    await _cache_set(cache_key, result, _TTL_GA4)
    return JSONResponse({"cached": False, **result})


# ---------------------------------------------------------------------------
# /api/kpi-library/metadata/bigquery/*
# ---------------------------------------------------------------------------


@router.get("/api/kpi-library/metadata/bigquery/datasets")
async def bq_datasets(
    request: Request,
    connection_id: str = Query(...),
    refresh: int = Query(0),
):
    _, _, project_id = await _require_user_and_project(request)
    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connection_id")

    conn = await _resolve_bq_connection(conn_uuid, project_id)

    cache_key = f"kpi:meta:bq:datasets:{connection_id}"
    if not refresh:
        cached = await _cache_get(cache_key)
        if cached is not None:
            return JSONResponse({"cached": True, **cached})

    if app_state.bq_connector is None:
        raise HTTPException(status_code=503, detail="BigQuery connector unavailable")

    result = await app_state.bq_connector.list_datasets(conn.service_account_encrypted, conn.project_id)
    await _cache_set(cache_key, result, _TTL_BQ)
    return JSONResponse({"cached": False, **result})


@router.get("/api/kpi-library/metadata/bigquery/tables")
async def bq_tables(
    request: Request,
    connection_id: str = Query(...),
    dataset: str = Query(...),
    refresh: int = Query(0),
):
    _, _, project_id = await _require_user_and_project(request)
    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connection_id")

    conn = await _resolve_bq_connection(conn_uuid, project_id)

    cache_key = f"kpi:meta:bq:tables:{connection_id}:{dataset}"
    if not refresh:
        cached = await _cache_get(cache_key)
        if cached is not None:
            return JSONResponse({"cached": True, **cached})

    if app_state.bq_connector is None:
        raise HTTPException(status_code=503, detail="BigQuery connector unavailable")

    result = await app_state.bq_connector.list_tables(
        conn.service_account_encrypted, conn.project_id, dataset
    )
    await _cache_set(cache_key, result, _TTL_BQ)
    return JSONResponse({"cached": False, **result})


@router.get("/api/kpi-library/metadata/bigquery/columns")
async def bq_columns(
    request: Request,
    connection_id: str = Query(...),
    dataset: str = Query(...),
    table: str = Query(...),
    refresh: int = Query(0),
):
    _, _, project_id = await _require_user_and_project(request)
    try:
        conn_uuid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid connection_id")

    conn = await _resolve_bq_connection(conn_uuid, project_id)

    cache_key = f"kpi:meta:bq:cols:{connection_id}:{dataset}:{table}"
    if not refresh:
        cached = await _cache_get(cache_key)
        if cached is not None:
            return JSONResponse({"cached": True, **cached})

    if app_state.bq_connector is None:
        raise HTTPException(status_code=503, detail="BigQuery connector unavailable")

    result = await app_state.bq_connector.get_table_schema(
        conn.service_account_encrypted, conn.project_id, dataset, table
    )
    await _cache_set(cache_key, result, _TTL_BQ)
    return JSONResponse({"cached": False, **result})
