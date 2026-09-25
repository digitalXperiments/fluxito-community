"""
Answer Audit Trail — Routes.

Exposes the `tool_call_audit` table to the authenticated user via:

HTML pages:
  GET /activity-log                     — Paginated list of every tool call
  GET /activity-log/{audit_id}          — Detail view: arguments + full response

JSON API:
  GET /api/activity-log                 — List (supports ?tool=&status=&source=&limit=&offset=)
  GET /api/activity-log/{audit_id}      — Single row
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import desc, select

import app.app_state as app_state
from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx
from app.models.audit import ToolCallAudit
from app.models.project import Project, ProjectMember
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


async def _require_user(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        user_uuid = uuid.UUID(user_ctx.user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_ctx, user_uuid


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/activity-log")
async def audit_page(request: Request):
    """
    Unified activity + audit page.

    Day-by-day breakdown of every AI tool call, grouped by platform and
    tool, interleaved with account events (sign-ins, connection changes).
    Each tool row links to ``/activity-log/{id}`` for the full arguments + response.

    Replaces the old ``/profile/activity`` page — both now share this
    single route and backing store (``tool_call_audit`` +
    ``activity_events``).
    """
    from collections import defaultdict
    from datetime import date, datetime, timedelta

    from app.models.activity import ActivityEvent

    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/activity-log", status_code=302)
    user_view = await _load_user_view(user_ctx)
    user_uuid = uuid.UUID(user_ctx.user_id)

    # ------- Config -------
    PLATFORM_LABELS = {
        "ga4": "Google Analytics 4",
        "gtm": "Google Tag Manager",
        "google_ads": "Google Ads",
        "meta": "Meta Ads",
        "tiktok": "TikTok Ads",
        "snap": "Snap Ads",
        "bigquery": "BigQuery",
        "redshift": "Redshift",
        "snowflake": "Snowflake",
        "amplitude": "Amplitude",
        "mixpanel": "Mixpanel",
        "posthog": "PostHog",
        "adobe": "Adobe Analytics",
    }

    def humanize_tool(name: str) -> str:
        if not name:
            return "Unknown"
        stripped = name
        for prefix in (
            "analytics_",
            "tagmanager_",
            "marketing_",
            "warehouse_",
            "dashboard_",
            "template_",
            "cross_platform_",
        ):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :]
                break
        return stripped.replace("_", " ").title()

    def _infer_platform(tool_name: str | None) -> str | None:
        if not tool_name:
            return None
        t = tool_name.lower()
        if t.startswith("tagmanager_") or t.startswith("gtm_"):
            return "gtm"
        if t.startswith("analytics_"):
            return "ga4"
        if t.startswith(("adwords_", "ads_", "google_ads_")):
            return "google_ads"
        if t.startswith(("warehouse_", "bigquery_", "bq_")):
            return "bigquery"
        if t.startswith("redshift_"):
            return "redshift"
        if t.startswith("snowflake_"):
            return "snowflake"
        if t.startswith("meta_"):
            return "meta"
        if t.startswith("tiktok_"):
            return "tiktok"
        if t.startswith("snap_"):
            return "snap"
        if t.startswith("amplitude_"):
            return "amplitude"
        if t.startswith("mixpanel_"):
            return "mixpanel"
        if t.startswith("posthog_"):
            return "posthog"
        if t.startswith("adobe_"):
            return "adobe"
        return None

    now = datetime.utcnow()
    today = now.date()
    window_days = 14
    window_start = now - timedelta(days=window_days)
    week_ago = now - timedelta(days=7)

    stats = {
        "total_week": 0,
        "writes_week": 0,
        "failures_week": 0,
        "sources_week": set(),
        "platforms_week": set(),
    }

    days: dict = {}
    account_events_by_day: dict[str, list] = defaultdict(list)

    # Optional EXPLICIT project filter via ?project_id=. We deliberately do NOT
    # fall back to the active_project_id cookie here: MCP tool calls are audited
    # against whichever project the AI client had active (resolved per-call from
    # Redis), which routinely differs from the project the web UI happens to have
    # selected — or is NULL when no project was set. Silently scoping the log to
    # the browser cookie hid the user's entire tool-call history. Default to
    # showing everything the user did; let project filtering be opt-in.
    active_project_id = request.query_params.get("project_id")
    tool_filter = request.query_params.get("tool")
    platform_filter = request.query_params.get("platform")

    db_factory = app_state.db_session_factory
    async with db_factory() as db:
        # Load user's projects for filter dropdown
        member_q = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_uuid)
            .order_by(Project.name)
        )
        user_projects = (await db.execute(member_q)).scalars().all()

        # Collect distinct tool names for filter dropdown (last 14 days)
        tool_names_q = (
            select(ToolCallAudit.tool_name)
            .where(ToolCallAudit.user_id == user_uuid)
            .where(ToolCallAudit.created_at >= window_start)
            .distinct()
            .order_by(ToolCallAudit.tool_name)
        )
        all_tool_names = [r[0] for r in (await db.execute(tool_names_q)).all()]

        # Collect distinct platforms for filter dropdown
        platform_q = (
            select(ToolCallAudit.platform)
            .where(ToolCallAudit.user_id == user_uuid)
            .where(ToolCallAudit.created_at >= window_start)
            .where(ToolCallAudit.platform.isnot(None))
            .distinct()
            .order_by(ToolCallAudit.platform)
        )
        all_platforms_raw = [r[0] for r in (await db.execute(platform_q)).all()]
        inferred_from_tools = {_infer_platform(tn) for tn in all_tool_names if _infer_platform(tn)}
        all_platforms = sorted(
            set(all_platforms_raw) | inferred_from_tools,
            key=lambda s: (PLATFORM_LABELS.get(s, s.replace("_", " ").title())),
        )
        platform_options = [(p, PLATFORM_LABELS.get(p, p.replace("_", " ").title())) for p in all_platforms]

        # ---- Load tool calls from the audit table (last N days) ----
        rows_q = (
            select(ToolCallAudit)
            .where(ToolCallAudit.user_id == user_uuid)
            .where(ToolCallAudit.created_at >= window_start)
            .order_by(desc(ToolCallAudit.created_at))
        )
        # Scope to active project when available
        if active_project_id:
            try:
                rows_q = rows_q.where(ToolCallAudit.project_id == uuid.UUID(active_project_id))
            except ValueError:
                pass
        if tool_filter:
            rows_q = rows_q.where(ToolCallAudit.tool_name == tool_filter)
        tool_rows = (await db.execute(rows_q)).scalars().all()

        # Apply platform filter in Python (platform may be inferred)
        if platform_filter:
            tool_rows = [
                r
                for r in tool_rows
                if (r.platform or _infer_platform(r.tool_name) or "other") == platform_filter
            ]

        for r in tool_rows:
            if not r.created_at:
                continue
            is_write = bool(r.is_write)
            is_issue = (r.status or "success") != "success"
            platform = r.platform or _infer_platform(r.tool_name) or "other"
            platform_label = PLATFORM_LABELS.get(platform, platform.replace("_", " ").title())
            source = r.source_client

            if r.created_at >= week_ago:
                stats["total_week"] += 1
                if is_write:
                    stats["writes_week"] += 1
                if is_issue:
                    stats["failures_week"] += 1
                if source:
                    stats["sources_week"].add(source)
                if platform and platform != "other":
                    stats["platforms_week"].add(platform)

            day_key = r.created_at.date().isoformat()
            if day_key not in days:
                days[day_key] = {
                    "date": r.created_at.date(),
                    "total": 0,
                    "writes": 0,
                    "issues": 0,
                    "sources": set(),
                    "platforms": {},
                }
            d = days[day_key]
            d["total"] += 1
            if is_write:
                d["writes"] += 1
            if is_issue:
                d["issues"] += 1
            if source:
                d["sources"].add(source)

            if platform not in d["platforms"]:
                d["platforms"][platform] = {
                    "slug": platform,
                    "label": platform_label,
                    "count": 0,
                    "writes": 0,
                    "issues": 0,
                    "calls": [],  # individual ToolCallAudit rows for linking
                }
            p = d["platforms"][platform]
            p["count"] += 1
            if is_write:
                p["writes"] += 1
            if is_issue:
                p["issues"] += 1

            # Keep one entry per call so each row is clickable to /activity-log/{id}.
            # Cap to avoid huge pages when a user has thousands of calls/day.
            if len(p["calls"]) < 50:
                p["calls"].append(
                    {
                        "id": str(r.id),
                        "tool_name": r.tool_name,
                        "display_name": humanize_tool(r.tool_name),
                        "is_write": is_write,
                        "is_issue": is_issue,
                        "status": r.status,
                        "source": source,
                        "summary": r.response_summary,
                        "duration_ms": r.duration_ms,
                        "time_str": r.created_at.strftime("%H:%M"),
                        "project_id": str(r.project_id) if r.project_id else None,
                        "project_name": next((p.name for p in user_projects if p.id == r.project_id), None),
                    }
                )

        # ---- Load account events (sign-ins, connections, etc.) ----
        event_q = (
            select(ActivityEvent)
            .where(ActivityEvent.user_id == user_uuid)
            .where(ActivityEvent.created_at >= window_start)
            .order_by(desc(ActivityEvent.created_at))
        )
        for r in (await db.execute(event_q)).scalars().all():
            if not r.created_at:
                continue
            day_key = r.created_at.date().isoformat()
            account_events_by_day[day_key].append(
                {
                    "event_type": r.event_type,
                    "description": r.description or "",
                    "time_str": r.created_at.strftime("%H:%M"),
                }
            )

    # ---- Finalize day list for the template ----
    def day_label(d: date) -> str:
        if d == today:
            return "Today"
        if d == today - timedelta(days=1):
            return "Yesterday"
        return d.strftime("%A, %b %-d")

    day_list = []
    all_day_keys = set(days.keys()) | set(account_events_by_day.keys())
    for day_key in sorted(all_day_keys, reverse=True):
        d = days.get(
            day_key,
            {
                "date": date.fromisoformat(day_key),
                "total": 0,
                "writes": 0,
                "issues": 0,
                "sources": set(),
                "platforms": {},
            },
        )
        d["key"] = day_key
        d["label"] = day_label(d["date"])
        d["sources_list"] = sorted(d["sources"])
        d["account_events"] = account_events_by_day.get(day_key, [])

        platforms_list = []
        for plat in d["platforms"].values():
            plat["calls"].sort(key=lambda c: c["time_str"], reverse=True)
            plat["unique_tools"] = len({c["tool_name"] for c in plat["calls"]})
            platforms_list.append(plat)
        platforms_list.sort(key=lambda p: -p["count"])
        d["platforms_list"] = platforms_list
        day_list.append(d)

    stats["sources_count"] = len(stats["sources_week"])
    stats["platforms_count"] = len(stats["platforms_week"])
    stats["sources_list"] = sorted(stats["sources_week"])

    return render(
        request,
        "audit.html",
        {
            "user": user_view,
            "stats": stats,
            "day_list": day_list,
            "window_days": window_days,
            "projects": user_projects,
            "selected_project_id": active_project_id,
            "all_tool_names": all_tool_names,
            "selected_tool": tool_filter,
            "platform_options": platform_options,
            "selected_platform": platform_filter,
        },
    )


@router.get("/activity-log/{audit_id}")
async def audit_detail_page(request: Request, audit_id: str):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        from urllib.parse import quote

        return RedirectResponse(
            f"/signin?next={quote(f'/activity-log/{audit_id}', safe='/')}", status_code=302
        )
    user_view = await _load_user_view(user_ctx)
    user_uuid = uuid.UUID(user_ctx.user_id)

    try:
        rid = uuid.UUID(audit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    db_factory = app_state.db_session_factory
    async with db_factory() as db:
        row = (
            await db.execute(
                select(ToolCallAudit).where(
                    ToolCallAudit.id == rid,
                    ToolCallAudit.user_id == user_uuid,
                )
            )
        ).scalar_one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    return render(
        request,
        "audit_detail.html",
        {
            "user": user_view,
            "row": row.to_dict(),
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/activity-log")
async def api_audit_list(
    request: Request,
    tool: str | None = Query(None),
    status: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    _, user_uuid = await _require_user(request)

    stmt = select(ToolCallAudit).where(ToolCallAudit.user_id == user_uuid)

    # Explicit project filter only (see audit_page for why we don't use the
    # active_project_id cookie — it silently hid the user's tool-call history).
    active_project_id = request.query_params.get("project_id")
    if active_project_id:
        try:
            stmt = stmt.where(ToolCallAudit.project_id == uuid.UUID(active_project_id))
        except ValueError:
            pass

    if tool:
        stmt = stmt.where(ToolCallAudit.tool_name == tool)
    if status:
        stmt = stmt.where(ToolCallAudit.status == status)
    if source:
        stmt = stmt.where(ToolCallAudit.source_client == source)
    stmt = stmt.order_by(desc(ToolCallAudit.created_at)).limit(limit).offset(offset)

    db_factory = app_state.db_session_factory
    async with db_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()

    return JSONResponse(
        {
            "rows": [
                # Strip heavy fields for list view
                {k: v for k, v in r.to_dict().items() if k not in ("response_preview",)}
                for r in rows
            ],
            "count": len(rows),
        }
    )


@router.get("/api/activity-log/{audit_id}")
async def api_audit_detail(request: Request, audit_id: str):
    _, user_uuid = await _require_user(request)
    try:
        rid = uuid.UUID(audit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    db_factory = app_state.db_session_factory
    async with db_factory() as db:
        row = (
            await db.execute(
                select(ToolCallAudit).where(
                    ToolCallAudit.id == rid,
                    ToolCallAudit.user_id == user_uuid,
                )
            )
        ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(row.to_dict())
