"""
Knowledge Base MCP tools.

Exposes the project's KPI catalog and Business Context to Claude as two
read-only tools. Claude should call these whenever:
  - the user asks about a metric/KPI by name or alias ("what's our CAC?")
  - the user asks a business question that benefits from context
    ("why did sessions drop last week?")
  - at the start of an analytics conversation to ground answers in the
    client's terminology and rules.

Both tools are project-scoped via the active ``ProjectContext`` set when
the user calls ``set_active_project`` in their MCP session.

KPI payload shape
-----------------
Each KPI returned by ``get_kpi`` / ``compute_kpi`` includes its identity
(name, slug, aliases, status, version), its definition (description,
business_question, interpretation_guide), its computation spec
(expression + bound inputs), and quality metadata (unit, direction,
target, expected range).

Only ``status='approved'`` KPIs with at least one bound input are
considered fully MCP-ready. Draft or input-less KPIs are still returned
but flagged via ``is_ready=false`` so Claude knows the formula cannot be
executed and should qualify its answer.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app.app_state as app_state
from app.auth.mcp_session_manager import no_active_project_response, require_project_ctx
from app.models.knowledge import KPI, BusinessContext
from app.services.kpi_executor import compute_kpi_by_slug

logger = logging.getLogger(__name__)

_KPI_NOTE_WITH_DATA = (
    "These KPIs are the client-specific source of truth. Use these "
    "definitions, aliases, units, and directions when answering — do not "
    "substitute generic definitions. Prefer KPIs with is_ready=true when "
    "reporting numbers; for is_ready=false, use the definition but qualify "
    "that the formula is not yet bound to data sources."
)
_KPI_NOTE_EMPTY = (
    "The KPI catalog is empty. The user can add KPIs at "
    "/kpi-library to customize how Claude interprets their metrics."
)
_CONTEXT_NOTE_WITH_DATA = (
    "Use this document to ground your answers in the client's "
    "actual business — industry, audience, terminology, rules."
)
_CONTEXT_NOTE_EMPTY = (
    "No business context has been written yet. The user can "
    "add one at /business-context to give Claude the background "
    "it needs for better analytics answers."
)


def _kpi_to_mcp_dict(k: KPI) -> dict[str, Any]:
    """Shape a KPI for the MCP payload — trimmed to what Claude needs."""
    is_ready = k.status == "approved" and bool(k.inputs) and bool(k.expression)
    return {
        "slug": k.slug,
        "name": k.name,
        "aliases": list(k.aliases or []),
        "status": k.status,
        "version": k.version,
        "is_ready": is_ready,
        "description": k.description,
        "business_question": k.business_question,
        "interpretation_guide": k.interpretation_guide,
        "category": k.category,
        "tags": list(k.tags or []),
        "expression": k.expression,
        "time_grain": k.time_grain,
        "unit": k.unit,
        "format": k.format_spec,
        "direction": k.direction,
        "target": (
            {"value": float(k.target_value), "type": k.target_type} if k.target_value is not None else None
        ),
        "expected_range": (
            {
                "min": float(k.expected_range_min) if k.expected_range_min is not None else None,
                "max": float(k.expected_range_max) if k.expected_range_max is not None else None,
            }
            if (k.expected_range_min is not None or k.expected_range_max is not None)
            else None
        ),
        "owner": k.owner,
        "source_of_truth_url": k.source_of_truth_url,
        "inputs": [
            {
                "key": i.key,
                "source": i.source,
                "connection_id": str(i.connection_id),
                "binding": dict(i.binding or {}),
            }
            for i in (k.inputs or [])
        ],
    }


def register_knowledge_tools(mcp_server: Any) -> None:
    @mcp_server.tool("list_kpis")
    async def list_kpis(status: str = "approved") -> dict[str, Any]:
        """
        Return a concise catalog of KPIs for the active project — slug,
        name, aliases, status, and a one-line description. Use this for
        discovery; call ``get_knowledge(action="get_kpi", params={"slug": "..."})`` for the full structured spec of a
        specific KPI.

        Args:
            status: Filter by lifecycle status. One of ``approved``
                (default), ``draft``, ``deprecated``, or ``all``.
        """
        project_ctx = require_project_ctx()
        if not project_ctx:
            return no_active_project_response()
        project_id = uuid.UUID(project_ctx.project_id)

        status = (status or "approved").strip().lower()
        valid = {"approved", "draft", "deprecated", "all"}
        if status not in valid:
            return {
                "error": True,
                "error_type": "bad_argument",
                "message": f"status must be one of {sorted(valid)}",
            }

        async with app_state.db_session_factory() as db:
            stmt = (
                select(KPI)
                .where(KPI.project_id == project_id)
                .options(selectinload(KPI.inputs))
                .order_by(KPI.name)
            )
            if status != "all":
                stmt = stmt.where(KPI.status == status)
            rows = (await db.execute(stmt)).scalars().all()

        kpis = [
            {
                "slug": k.slug,
                "name": k.name,
                "aliases": list(k.aliases or []),
                "status": k.status,
                "category": k.category,
                "unit": k.unit,
                "direction": k.direction,
                "is_ready": k.status == "approved" and bool(k.inputs) and bool(k.expression),
                "short_description": (k.description or "").split("\n", 1)[0][:200],
            }
            for k in rows
        ]
        return {
            "scope": "project",
            "project": project_ctx.project_name,
            "status_filter": status,
            "count": len(kpis),
            "kpis": kpis,
            "hint": 'Call get_knowledge(action="get_kpi", params={"slug": "..."}) for the full spec or get_knowledge(action="compute_kpi", params={"slug": "...", ...}) to run the formula.',
        }

    @mcp_server.tool("get_kpi")
    async def get_kpi(slug: str) -> dict[str, Any]:
        """
        Return the full structured spec for a single KPI — identity,
        definition, computation spec (expression + bound inputs), and
        quality metadata. Match is case-insensitive on ``slug``.

        Use this when the user mentions a specific metric by name and
        you want to know exactly how the project defines it, what data
        sources feed it, and whether the formula is ready to compute.
        """
        project_ctx = require_project_ctx()
        if not project_ctx:
            return no_active_project_response()
        if not slug or not slug.strip():
            return {"error": True, "error_type": "bad_argument", "message": "slug is required"}

        project_id = uuid.UUID(project_ctx.project_id)
        slug_lc = slug.strip().lower()

        async with app_state.db_session_factory() as db:
            stmt = (
                select(KPI)
                .where(KPI.project_id == project_id)
                .where(sa_func.lower(KPI.slug) == slug_lc)
                .options(selectinload(KPI.inputs))
            )
            kpi = (await db.execute(stmt)).scalar_one_or_none()

        if not kpi:
            return {
                "error": True,
                "error_type": "not_found",
                "message": f"No KPI with slug '{slug}' in this project.",
            }

        return {
            "scope": "project",
            "project": project_ctx.project_name,
            "kpi": _kpi_to_mcp_dict(kpi),
        }

    @mcp_server.tool("compute_kpi")
    async def compute_kpi(
        slug: str,
        date_range_start: str = "30daysAgo",
        date_range_end: str = "today",
    ) -> dict[str, Any]:
        """
        Execute a KPI's formula against its bound sources and return the
        scalar value.

        CALL THIS when the user asks for the current value of a KPI (e.g.
        "what's our CAC this month?"). Don't invent numbers — always
        prefer ``get_knowledge(action="compute_kpi")`` over composing the formula yourself from
        raw analytics calls.

        Args:
            slug: Case-insensitive KPI slug (see get_knowledge(action='list_kpis')).
            date_range_start: GA4 relative (``30daysAgo``) or ISO
                ``YYYY-MM-DD``. Only affects time-sensitive inputs
                (GA4 today; BigQuery inputs apply their own filters).
            date_range_end: GA4 relative (``today``) or ISO.

        Returns:
            {
                "value": float | None,
                "mode": "push_down" | "pull_up" | null,
                "expression": str,
                "inputs": { <key>: float, ... },
                "warnings": [str, ...],
                "error": str | null,
                "kpi": { slug, name, unit, format, direction, ... }
            }
        """
        project_ctx = require_project_ctx()
        if not project_ctx:
            return no_active_project_response()
        if not slug or not slug.strip():
            return {"error": True, "error_type": "bad_argument", "message": "slug is required"}

        project_id = uuid.UUID(project_ctx.project_id)

        result = await compute_kpi_by_slug(slug.strip(), project_id, date_range_start, date_range_end)

        # Attach a trimmed KPI summary so Claude can render the answer
        # with unit / direction / format without a second round-trip.
        async with app_state.db_session_factory() as db:
            stmt = (
                select(KPI)
                .where(KPI.project_id == project_id)
                .where(sa_func.lower(KPI.slug) == slug.strip().lower())
            )
            kpi = (await db.execute(stmt)).scalar_one_or_none()
        if kpi is not None:
            result["kpi"] = {
                "slug": kpi.slug,
                "name": kpi.name,
                "unit": kpi.unit,
                "format": kpi.format_spec,
                "direction": kpi.direction,
                "target": float(kpi.target_value) if kpi.target_value is not None else None,
            }
        return result

    @mcp_server.tool("get_business_context")
    async def get_business_context() -> dict[str, Any]:
        """
        Returns the Business Context document for the active project —
        a Markdown file describing the industry, audience, goals,
        seasonality, internal terminology, and any business rules Claude
        should use when answering analytics questions.

        CALL THIS at the start of a new analytics conversation, or
        whenever the user asks a question that depends on business
        context you don't already have (e.g. "why did we see a spike?",
        "is this campaign on target?").

        Returns:
            {
                "scope": "project",
                "project": str,
                "content": str,   # raw Markdown, possibly empty
                "has_content": bool
            }
        """
        project_ctx = require_project_ctx()
        if not project_ctx:
            return no_active_project_response()

        project_id = uuid.UUID(project_ctx.project_id)

        async with app_state.db_session_factory() as db:
            stmt = select(BusinessContext).where(BusinessContext.project_id == project_id)
            result = await db.execute(stmt)
            doc = result.scalar_one_or_none()

        content = doc.content if doc else ""
        has_content = bool(content.strip())
        return {
            "scope": "project",
            "project": project_ctx.project_name,
            "content": content,
            "has_content": has_content,
            "note": _CONTEXT_NOTE_WITH_DATA if has_content else _CONTEXT_NOTE_EMPTY,
        }
