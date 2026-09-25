"""
Automated Test Flows — REST API Routes
======================================

Web UI + JSON endpoints for Phase 4 "Automated Test Flows" (tag testing):
saved, replayable browser flows with per-step dataLayer / vendor-beacon
assertions, plus the project's reusable vendor catalog.

HTML pages (all gated to project members; sidebar "Audits" section):
  GET /audits/flows                          — flow list
  GET /audits/flows/new                      — flow builder (new)
  GET /audits/flows/{flow_id}                — flow builder (edit)
  GET /audits/flows/{flow_id}/runs/{run_id}  — single run detail
  GET /audits/vendors                        — vendor catalog manager

JSON API:
  Vendors
    GET    /api/audit/vendors
    POST   /api/audit/vendors
    PUT    /api/audit/vendors/{vendor_id}
    DELETE /api/audit/vendors/{vendor_id}
  Flows
    GET    /api/audit/flows
    POST   /api/audit/flows
    GET    /api/audit/flows/{flow_id}
    PUT    /api/audit/flows/{flow_id}
    DELETE /api/audit/flows/{flow_id}
    GET    /api/audit/flows/{flow_id}/runs
    GET    /api/audit/flows/{flow_id}/runs/{run_id}
    POST   /api/audit/flows/{flow_id}/run       — fire-and-forget manual run
    POST   /api/audit/flows/{flow_id}/toggle    — {enabled: bool}

Auth mirrors auditing_routes / tracking_plan_routes: resolve the signed-in
user, resolve the active project, and require project membership. There is no
`tools.automation` entitlement nor an `editor` role in this codebase (roles are
owner/admin/member), so — per the task's "else same gate" fallback — every
endpoint requires project membership and mutations are not further role-gated.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import desc, func, select

import app.app_state as app_state
from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx
from app.api.project_routes import ensure_active_project
from app.models.project import ProjectMember
from app.models.test_flows import AuditVendor, TestFlow, TestFlowRun
from app.tag_testing.flow_runner.executor import _resolve_url, is_safe_http_url
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

# Step-schema whitelists (server-side validation mirrors the executor).
_VALID_ACTIONS = {"navigate", "click", "type", "wait"}
_VALID_OPS = {"equals", "contains", "regex", "exists", "not_empty"}
_VALID_WHEN = {"anytime", "at_step"}
_VALID_MODE = {"must", "must_not"}
_VALID_DEVICE = {"desktop", "mobile_web"}
_MAX_STEPS = 50
_WAIT_CAP_MS = 30_000

# In-process guard for manual "Run now": run_flow commits its TestFlowRun row
# only at the very end of a run, so a cross-session DB query for a
# status='running' row can't see an in-flight run. We therefore track flows
# with a manual run in flight in-process. This is per-replica (manual runs are
# triggered from one API replica), which is sufficient for the duplicate-click
# guard the spec asks for.
_RUNNING_FLOWS: set[str] = set()
_RUNNING_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Auth / project resolution
# ---------------------------------------------------------------------------


async def _resolve(request: Request) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (user_uuid, project_uuid) requiring project membership.

    Raises HTTP 401 (unauthenticated), 400 (no active project), or 403
    (authenticated but not a member of the active project).
    """
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        user_uuid = uuid.UUID(user_ctx.user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized")

    pid_str = await ensure_active_project(request, user_ctx.user_id)
    if not pid_str:
        raise HTTPException(status_code=400, detail="No active project")
    project_uuid = uuid.UUID(pid_str)

    async with app_state.db_session_factory() as db:
        member = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_uuid,
                    ProjectMember.user_id == user_uuid,
                    ProjectMember.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=403, detail="Not a project member")
    # Same surface as the audit tools: viewing needs analysis:read, creating /
    # editing / running / deleting flows and vendors needs analysis:write.
    from app.auth.web_guards import require_domain_permission

    level = "read" if request.method in ("GET", "HEAD") else "write"
    await require_domain_permission(str(user_uuid), str(project_uuid), "analysis", level)
    return user_uuid, project_uuid


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class _ValidationError(ValueError):
    """Raised when a submitted flow/vendor payload is invalid."""


# Matches a single, well-formed email address with no control characters
# (embedded newlines would be a header-injection vector at send time).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _require_http_url(value: str, field: str) -> str:
    v = (value or "").strip()
    if not (v.startswith("http://") or v.startswith("https://")):
        raise _ValidationError(f"{field} must be an http(s) URL")
    if not is_safe_http_url(v):
        raise _ValidationError(
            f"{field} must point to a public host (internal/loopback addresses are blocked)"
        )
    return v


def _validate_checks(raw: object, container: str) -> list[dict]:
    """Validate a list of field/param checks. Returns a normalized list."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _ValidationError(f"{container} must be a list")
    out: list[dict] = []
    for i, chk in enumerate(raw):
        if not isinstance(chk, dict):
            raise _ValidationError(f"{container}[{i}] must be an object")
        key = chk.get("key")
        if not isinstance(key, str) or not key.strip():
            raise _ValidationError(f"{container}[{i}].key is required")
        op = chk.get("op") or ("exists" if chk.get("value") in (None, "") else "equals")
        if op not in _VALID_OPS:
            raise _ValidationError(f"{container}[{i}].op '{op}' is not one of {sorted(_VALID_OPS)}")
        norm = {"key": key.strip(), "op": op}
        if "value" in chk and chk["value"] is not None:
            norm["value"] = chk["value"]
        out.append(norm)
    return out


def _validate_assertions(raw: object, vendor_ids: set[str], step_no: int) -> dict:
    """Validate a single step's assertions block. Returns a normalized dict."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _ValidationError(f"step {step_no}: assertions must be an object")

    norm: dict = {}

    dl_events = raw.get("datalayer_events")
    if dl_events is not None:
        if not isinstance(dl_events, list):
            raise _ValidationError(f"step {step_no}: datalayer_events must be a list")
        norm_dl: list[dict] = []
        for j, ev in enumerate(dl_events):
            if not isinstance(ev, dict):
                raise _ValidationError(f"step {step_no}: datalayer_events[{j}] must be an object")
            event = ev.get("event")
            if not isinstance(event, str) or not event.strip():
                raise _ValidationError(f"step {step_no}: datalayer_events[{j}].event is required")
            mode = ev.get("mode", "must")
            if mode not in _VALID_MODE:
                raise _ValidationError(f"step {step_no}: datalayer_events[{j}].mode invalid")
            when = ev.get("when", "anytime")
            if when not in _VALID_WHEN:
                raise _ValidationError(f"step {step_no}: datalayer_events[{j}].when invalid")
            norm_dl.append(
                {
                    "event": event.strip(),
                    "mode": mode,
                    "when": when,
                    "fields": _validate_checks(
                        ev.get("fields"), f"step {step_no}: datalayer_events[{j}].fields"
                    ),
                }
            )
        norm["datalayer_events"] = norm_dl

    vendor_reqs = raw.get("vendor_requests")
    if vendor_reqs is not None:
        if not isinstance(vendor_reqs, list):
            raise _ValidationError(f"step {step_no}: vendor_requests must be a list")
        norm_vr: list[dict] = []
        for j, vr in enumerate(vendor_reqs):
            if not isinstance(vr, dict):
                raise _ValidationError(f"step {step_no}: vendor_requests[{j}] must be an object")
            vid = str(vr.get("vendor_id") or "").strip()
            if not vid:
                raise _ValidationError(f"step {step_no}: vendor_requests[{j}].vendor_id is required")
            if vid not in vendor_ids:
                raise _ValidationError(
                    f"step {step_no}: vendor_requests[{j}] references unknown vendor {vid}"
                )
            mode = vr.get("mode", "must")
            if mode not in _VALID_MODE:
                raise _ValidationError(f"step {step_no}: vendor_requests[{j}].mode invalid")
            when = vr.get("when", "anytime")
            if when not in _VALID_WHEN:
                raise _ValidationError(f"step {step_no}: vendor_requests[{j}].when invalid")
            norm_vr.append(
                {
                    "vendor_id": vid,
                    "mode": mode,
                    "when": when,
                    "params": _validate_checks(
                        vr.get("params"), f"step {step_no}: vendor_requests[{j}].params"
                    ),
                }
            )
        norm["vendor_requests"] = norm_vr

    return norm


def _validate_steps(raw: object, base_url: str, vendor_ids: set[str]) -> list[dict]:
    """Validate + normalize the `steps` array. Raises _ValidationError."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _ValidationError("steps must be a list")
    if len(raw) > _MAX_STEPS:
        raise _ValidationError(f"a flow may have at most {_MAX_STEPS} steps (got {len(raw)})")

    out: list[dict] = []
    for idx, step in enumerate(raw):
        step_no = idx + 1
        if not isinstance(step, dict):
            raise _ValidationError(f"step {step_no} must be an object")
        action = (step.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            raise _ValidationError(
                f"step {step_no}: action '{action}' is not one of {sorted(_VALID_ACTIONS)}"
            )

        norm: dict = {"action": action}
        if step.get("label"):
            norm["label"] = str(step["label"])

        if action == "navigate":
            url = (step.get("url") or "").strip()
            if url:
                # Parse the scheme rather than substring-checking "://" — schemes
                # like data:/javascript: have no "://" and would otherwise slip
                # through to the relative branch and be navigated to server-side.
                scheme = urlsplit(url).scheme.lower()
                if scheme in ("http", "https"):
                    if not is_safe_http_url(url):
                        raise _ValidationError(f"step {step_no}: navigate url must point to a public host")
                    norm["url"] = url
                elif scheme:
                    raise _ValidationError(f"step {step_no}: navigate url must be an http(s) URL")
                else:
                    # Relative URL — validate the target it resolves to against
                    # base_url (which is itself http(s)+public-host validated).
                    if base_url and not is_safe_http_url(_resolve_url(base_url, url)):
                        raise _ValidationError(f"step {step_no}: navigate url resolves to a disallowed host")
                    norm["url"] = url
            elif not base_url:
                raise _ValidationError(f"step {step_no}: navigate needs a url when the flow has no base_url")
        elif action in ("click", "type"):
            selector = step.get("selector")
            if not isinstance(selector, str) or not selector.strip():
                raise _ValidationError(f"step {step_no}: {action} requires a selector")
            norm["selector"] = selector.strip()
            if action == "type":
                norm["text"] = str(step.get("text") or "")
        elif action == "wait":
            try:
                ms = int(step.get("ms") or 0)
            except (TypeError, ValueError):
                raise _ValidationError(f"step {step_no}: wait ms must be an integer")
            if ms < 0:
                raise _ValidationError(f"step {step_no}: wait ms must be >= 0")
            norm["ms"] = min(ms, _WAIT_CAP_MS)  # cap at 30s (executor caps too)

        assertions = _validate_assertions(step.get("assertions"), vendor_ids, step_no)
        if assertions:
            norm["assertions"] = assertions
        out.append(norm)
    return out


def _validate_cron(cron: str, timezone: str) -> None:
    """Validate a cron expression with the same parser APScheduler uses.

    First a cheap shape check, then the authoritative CronTrigger.from_crontab
    build (which is what the scheduler will call at job-add time).
    """
    from app.scheduling.cron import validate_cron_expression

    ok, err = validate_cron_expression(cron)
    if not ok:
        raise _ValidationError(f"invalid schedule_cron: {err}")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron, timezone=timezone or "UTC")
    except Exception as exc:  # pragma: no cover - defensive
        raise _ValidationError(f"invalid schedule_cron: {exc}")


def _normalize_notify(raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _ValidationError("notify must be an object")
    out: dict = {}
    wh = raw.get("slack_webhook_ids")
    if wh is not None:
        if not isinstance(wh, list):
            raise _ValidationError("notify.slack_webhook_ids must be a list")
        out["slack_webhook_ids"] = [str(x) for x in wh if x]
    if raw.get("email_sender_id"):
        out["email_sender_id"] = str(raw["email_sender_id"])
    rec = raw.get("recipients")
    if rec is not None:
        if not isinstance(rec, list):
            raise _ValidationError("notify.recipients must be a list")
        recipients: list[str] = []
        for x in rec:
            addr = str(x).strip()
            if not addr:
                continue
            # Reject control characters (embedded \n survives .strip() and is a
            # header-injection vector) and non-email-shaped values.
            if any(ord(c) < 32 for c in addr) or not _EMAIL_RE.match(addr):
                raise _ValidationError(f"notify.recipients contains an invalid email address: {addr!r}")
            recipients.append(addr)
        out["recipients"] = recipients
    return out


def _normalize_groups(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _ValidationError("groups must be a list")
    return [str(x).strip() for x in raw if str(x).strip()]


async def _project_vendor_ids(db, project_id: uuid.UUID) -> set[str]:
    rows = (
        (await db.execute(select(AuditVendor.id).where(AuditVendor.project_id == project_id))).scalars().all()
    )
    return {str(v) for v in rows}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


async def _audit_nav_context(db, project_id: uuid.UUID) -> dict:
    """Counts for the Audit sub-nav tabs + rule-book count for the page head."""
    from app.tag_testing.rule_books.manifest import list_platforms_summary

    flow_count = (
        await db.execute(select(func.count()).select_from(TestFlow).where(TestFlow.project_id == project_id))
    ).scalar_one()
    vendor_count = (
        await db.execute(
            select(func.count()).select_from(AuditVendor).where(AuditVendor.project_id == project_id)
        )
    ).scalar_one()
    return {
        "audit_flow_count": int(flow_count or 0),
        "audit_vendor_count": int(vendor_count or 0),
        "audit_rulebook_count": len(list_platforms_summary()),
    }


@router.get("/audits/flows")
async def flows_page(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/audits/flows", status_code=302)
    pid_str = await ensure_active_project(request, user_ctx.user_id)
    if not pid_str:
        return RedirectResponse("/projects", status_code=302)

    user_view = await _load_user_view(user_ctx)
    project_id = uuid.UUID(pid_str)

    async with app_state.db_session_factory() as db:
        flows = (
            (
                await db.execute(
                    select(TestFlow)
                    .where(TestFlow.project_id == project_id)
                    .order_by(desc(TestFlow.updated_at))
                )
            )
            .scalars()
            .all()
        )
        nav = await _audit_nav_context(db, project_id)

    return render(
        request,
        "audits/flows.html",
        {
            **nav,
            "user": user_view,
            "flows": [f.to_dict() for f in flows],
            "page_title": "Test Flows — Fluxito",
            "active": "audit_flows",
        },
    )


@router.get("/audits/flows/new")
async def flow_builder_new_page(request: Request):
    return await _render_builder(request, flow_id=None)


@router.get("/audits/flows/{flow_id}")
async def flow_builder_edit_page(request: Request, flow_id: str):
    return await _render_builder(request, flow_id=flow_id)


async def _render_builder(request: Request, flow_id: str | None):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        nxt = f"/audits/flows/{flow_id}" if flow_id else "/audits/flows/new"
        return RedirectResponse(f"/signin?next={nxt}", status_code=302)
    pid_str = await ensure_active_project(request, user_ctx.user_id)
    if not pid_str:
        return RedirectResponse("/projects", status_code=302)

    user_view = await _load_user_view(user_ctx)
    project_id = uuid.UUID(pid_str)

    flow_dict: dict | None = None
    async with app_state.db_session_factory() as db:
        if flow_id:
            try:
                fid = uuid.UUID(flow_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="Invalid flow ID")
            flow = await db.get(TestFlow, fid)
            if flow is None or flow.project_id != project_id:
                raise HTTPException(status_code=404, detail="Test flow not found")
            flow_dict = flow.to_dict()

        vendors = (
            (
                await db.execute(
                    select(AuditVendor).where(AuditVendor.project_id == project_id).order_by(AuditVendor.name)
                )
            )
            .scalars()
            .all()
        )

    return render(
        request,
        "audits/flow_builder.html",
        {
            "user": user_view,
            "flow": flow_dict,
            "vendors": [v.to_dict() for v in vendors],
            "page_title": ("Edit Test Flow" if flow_dict else "New Test Flow") + " — Fluxito",
            "active": "audit_flows",
        },
    )


@router.get("/audits/flows/{flow_id}/runs/{run_id}")
async def flow_run_page(request: Request, flow_id: str, run_id: str):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(f"/signin?next=/audits/flows/{flow_id}/runs/{run_id}", status_code=302)
    pid_str = await ensure_active_project(request, user_ctx.user_id)
    if not pid_str:
        return RedirectResponse("/projects", status_code=302)

    user_view = await _load_user_view(user_ctx)
    project_id = uuid.UUID(pid_str)

    try:
        fid = uuid.UUID(flow_id)
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid id")

    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
        run = await db.get(TestFlowRun, rid)
        if run is None or run.flow_id != fid or run.project_id != project_id:
            raise HTTPException(status_code=404, detail="Run not found")

    return render(
        request,
        "audits/flow_run.html",
        {
            "user": user_view,
            "flow": flow.to_dict(),
            "run": run.to_dict(),
            "page_title": f"Run — {flow.name} — Fluxito",
            "active": "audit_flows",
        },
    )


@router.get("/audits/vendors")
async def vendors_page(request: Request):
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/audits/vendors", status_code=302)
    pid_str = await ensure_active_project(request, user_ctx.user_id)
    if not pid_str:
        return RedirectResponse("/projects", status_code=302)

    user_view = await _load_user_view(user_ctx)
    project_id = uuid.UUID(pid_str)

    from app.tag_testing.vendor_catalog import TP_VENDOR_CATALOG

    async with app_state.db_session_factory() as db:
        vendors = (
            (
                await db.execute(
                    select(AuditVendor).where(AuditVendor.project_id == project_id).order_by(AuditVendor.name)
                )
            )
            .scalars()
            .all()
        )
        nav = await _audit_nav_context(db, project_id)

    return render(
        request,
        "audits/vendors.html",
        {
            **nav,
            "user": user_view,
            "vendors": [v.to_dict() for v in vendors],
            "catalog": TP_VENDOR_CATALOG,
            "page_title": "Audit Vendors — Fluxito",
            "active": "audit_vendors",
        },
    )


# ---------------------------------------------------------------------------
# JSON API — Vendors
# ---------------------------------------------------------------------------


@router.get("/api/audit/vendors")
async def api_list_vendors(request: Request):
    _user, project_id = await _resolve(request)
    async with app_state.db_session_factory() as db:
        vendors = (
            (
                await db.execute(
                    select(AuditVendor).where(AuditVendor.project_id == project_id).order_by(AuditVendor.name)
                )
            )
            .scalars()
            .all()
        )
    return JSONResponse({"vendors": [v.to_dict() for v in vendors], "count": len(vendors)})


@router.post("/api/audit/vendors")
async def api_create_vendor(request: Request):
    user_uuid, project_id = await _resolve(request)
    body = await request.json()
    try:
        vendor = _vendor_from_body(body, project_id, user_uuid)
    except _ValidationError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=400)

    async with app_state.db_session_factory() as db:
        # Enforce (project_id, slug) uniqueness with a friendly error.
        exists = (
            await db.execute(
                select(AuditVendor.id).where(
                    AuditVendor.project_id == project_id, AuditVendor.slug == vendor.slug
                )
            )
        ).scalar_one_or_none()
        if exists:
            return JSONResponse(
                {"error": True, "message": f"A vendor with slug '{vendor.slug}' already exists."},
                status_code=409,
            )
        db.add(vendor)
        await db.commit()
        await db.refresh(vendor)
    return JSONResponse(vendor.to_dict(), status_code=201)


@router.put("/api/audit/vendors/{vendor_id}")
async def api_update_vendor(request: Request, vendor_id: str):
    user_uuid, project_id = await _resolve(request)
    try:
        vid = uuid.UUID(vendor_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid vendor ID")
    body = await request.json()

    async with app_state.db_session_factory() as db:
        vendor = await db.get(AuditVendor, vid)
        if vendor is None or vendor.project_id != project_id:
            raise HTTPException(status_code=404, detail="Vendor not found")
        try:
            _apply_vendor_body(vendor, body)
        except _ValidationError as exc:
            return JSONResponse({"error": True, "message": str(exc)}, status_code=400)
        # Uniqueness on slug (excluding self).
        clash = (
            await db.execute(
                select(AuditVendor.id).where(
                    AuditVendor.project_id == project_id,
                    AuditVendor.slug == vendor.slug,
                    AuditVendor.id != vid,
                )
            )
        ).scalar_one_or_none()
        if clash:
            return JSONResponse(
                {"error": True, "message": f"A vendor with slug '{vendor.slug}' already exists."},
                status_code=409,
            )
        await db.commit()
        await db.refresh(vendor)
    return JSONResponse(vendor.to_dict())


@router.delete("/api/audit/vendors/{vendor_id}")
async def api_delete_vendor(request: Request, vendor_id: str):
    _user, project_id = await _resolve(request)
    try:
        vid = uuid.UUID(vendor_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid vendor ID")
    async with app_state.db_session_factory() as db:
        vendor = await db.get(AuditVendor, vid)
        if vendor is None or vendor.project_id != project_id:
            raise HTTPException(status_code=404, detail="Vendor not found")
        await db.delete(vendor)
        await db.commit()
    return JSONResponse({"ok": True, "deleted": str(vid)})


def _vendor_from_body(body: dict, project_id: uuid.UUID, user_uuid: uuid.UUID) -> AuditVendor:
    name = (body.get("name") or "").strip()
    if not name:
        raise _ValidationError("name is required")
    slug = (body.get("slug") or "").strip().lower()
    if not slug:
        raise _ValidationError("slug is required")
    url_pattern = (body.get("url_pattern") or "").strip()
    if not url_pattern:
        raise _ValidationError("url_pattern is required")
    params = body.get("params")
    if params is not None and not isinstance(params, list):
        raise _ValidationError("params must be a list")
    return AuditVendor(
        project_id=project_id,
        name=name,
        slug=slug,
        url_pattern=url_pattern,
        description=(body.get("description") or None),
        params=params or [],
        catalog_slug=(body.get("catalog_slug") or None),
        created_by=user_uuid,
    )


def _apply_vendor_body(vendor: AuditVendor, body: dict) -> None:
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            raise _ValidationError("name cannot be empty")
        vendor.name = name
    if "slug" in body:
        slug = (body.get("slug") or "").strip().lower()
        if not slug:
            raise _ValidationError("slug cannot be empty")
        vendor.slug = slug
    if "url_pattern" in body:
        url_pattern = (body.get("url_pattern") or "").strip()
        if not url_pattern:
            raise _ValidationError("url_pattern cannot be empty")
        vendor.url_pattern = url_pattern
    if "description" in body:
        vendor.description = body.get("description") or None
    if "params" in body:
        params = body.get("params")
        if params is not None and not isinstance(params, list):
            raise _ValidationError("params must be a list")
        vendor.params = params or []
    if "catalog_slug" in body:
        vendor.catalog_slug = body.get("catalog_slug") or None


# ---------------------------------------------------------------------------
# JSON API — Flows
# ---------------------------------------------------------------------------


@router.get("/api/audit/flows")
async def api_list_flows(request: Request):
    _user, project_id = await _resolve(request)
    async with app_state.db_session_factory() as db:
        flows = (
            (
                await db.execute(
                    select(TestFlow)
                    .where(TestFlow.project_id == project_id)
                    .order_by(desc(TestFlow.updated_at))
                )
            )
            .scalars()
            .all()
        )
        # Latest run per flow (for the assertions summary column).
        latest_by_flow: dict[uuid.UUID, TestFlowRun] = {}
        if flows:
            flow_ids = [f.id for f in flows]
            runs = (
                (
                    await db.execute(
                        select(TestFlowRun)
                        .where(TestFlowRun.flow_id.in_(flow_ids))
                        .order_by(TestFlowRun.flow_id, desc(TestFlowRun.started_at))
                    )
                )
                .scalars()
                .all()
            )
            for r in runs:
                latest_by_flow.setdefault(r.flow_id, r)

    out = []
    for f in flows:
        d = f.to_dict()
        lr = latest_by_flow.get(f.id)
        d["latest_run"] = (
            {
                "id": str(lr.id),
                "status": lr.status,
                "assertions_total": lr.assertions_total,
                "assertions_passed": lr.assertions_passed,
                "started_at": lr.started_at.isoformat() if lr.started_at else None,
                "finished_at": lr.finished_at.isoformat() if lr.finished_at else None,
            }
            if lr
            else None
        )
        out.append(d)
    return JSONResponse({"flows": out, "count": len(out)})


@router.post("/api/audit/flows")
async def api_create_flow(request: Request):
    user_uuid, project_id = await _resolve(request)
    body = await request.json()

    async with app_state.db_session_factory() as db:
        vendor_ids = await _project_vendor_ids(db, project_id)
        try:
            fields = _flow_fields_from_body(body, vendor_ids, is_create=True)
        except _ValidationError as exc:
            return JSONResponse({"error": True, "message": str(exc)}, status_code=400)

        flow = TestFlow(project_id=project_id, created_by=user_uuid, **fields)
        db.add(flow)
        await db.flush()

        # Schedule + persist next_run_at.
        _sync_and_stamp(flow)
        await db.commit()
        await db.refresh(flow)
    return JSONResponse(flow.to_dict(), status_code=201)


@router.get("/api/audit/flows/{flow_id}")
async def api_get_flow(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")
    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
    return JSONResponse(flow.to_dict())


@router.put("/api/audit/flows/{flow_id}")
async def api_update_flow(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")
    body = await request.json()

    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
        vendor_ids = await _project_vendor_ids(db, project_id)
        try:
            _apply_flow_body(flow, body, vendor_ids)
        except _ValidationError as exc:
            return JSONResponse({"error": True, "message": str(exc)}, status_code=400)

        _sync_and_stamp(flow)
        await db.commit()
        await db.refresh(flow)
    return JSONResponse(flow.to_dict())


@router.delete("/api/audit/flows/{flow_id}")
async def api_delete_flow(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")
    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
        await db.delete(flow)
        await db.commit()

    # Remove any scheduled job for this flow.
    try:
        from app.scheduling.service import remove_flow_job

        remove_flow_job(fid)
    except Exception:
        logger.warning("failed to remove flow job for %s", fid, exc_info=True)

    return JSONResponse({"ok": True, "deleted": str(fid)})


@router.get("/api/audit/flows/{flow_id}/runs")
async def api_list_flow_runs(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")
    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
        runs = (
            (
                await db.execute(
                    select(TestFlowRun)
                    .where(TestFlowRun.flow_id == fid)
                    .order_by(desc(TestFlowRun.started_at))
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    return JSONResponse({"runs": [r.to_dict() for r in runs], "count": len(runs)})


@router.get("/api/audit/flows/{flow_id}/runs/{run_id}")
async def api_get_flow_run(request: Request, flow_id: str, run_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid id")
    async with app_state.db_session_factory() as db:
        run = await db.get(TestFlowRun, rid)
        if run is None or run.flow_id != fid or run.project_id != project_id:
            raise HTTPException(status_code=404, detail="Run not found")
    return JSONResponse(run.to_dict())


@router.post("/api/audit/flows/{flow_id}/run")
async def api_run_flow_now(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")

    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")

    key = str(fid)
    async with _RUNNING_LOCK:
        if key in _RUNNING_FLOWS:
            return JSONResponse(
                {"error": True, "message": "A run for this flow is already in progress."},
                status_code=409,
            )
        _RUNNING_FLOWS.add(key)

    from app.tag_testing.flow_runner.service import run_flow

    async def _runner() -> None:
        try:
            await run_flow(str(fid), trigger="manual")
        except Exception:
            logger.exception("manual flow run failed for %s", fid)
        finally:
            async with _RUNNING_LOCK:
                _RUNNING_FLOWS.discard(key)

    # Fire-and-forget: run_flow owns TestFlowRun row creation (and its own
    # Semaphore(2)). It commits the run row only at the end, so we can't
    # return the run id synchronously — the UI polls GET .../runs for it.
    asyncio.create_task(_runner())
    return JSONResponse({"ok": True, "status": "started", "flow_id": key}, status_code=202)


@router.post("/api/audit/flows/{flow_id}/toggle")
async def api_toggle_flow(request: Request, flow_id: str):
    _user, project_id = await _resolve(request)
    try:
        fid = uuid.UUID(flow_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid flow ID")
    body = await request.json()
    enabled = bool(body.get("enabled"))

    async with app_state.db_session_factory() as db:
        flow = await db.get(TestFlow, fid)
        if flow is None or flow.project_id != project_id:
            raise HTTPException(status_code=404, detail="Test flow not found")
        flow.enabled = enabled
        _sync_and_stamp(flow)
        await db.commit()
        await db.refresh(flow)
    return JSONResponse(flow.to_dict())


# ---------------------------------------------------------------------------
# Flow body → column mapping
# ---------------------------------------------------------------------------


def _flow_fields_from_body(body: dict, vendor_ids: set[str], is_create: bool) -> dict:
    """Validate a create payload and return kwargs for the TestFlow ctor."""
    name = (body.get("name") or "").strip()
    if not name:
        raise _ValidationError("name is required")

    device = (body.get("device") or "desktop").strip()
    if device not in _VALID_DEVICE:
        raise _ValidationError(f"device must be one of {sorted(_VALID_DEVICE)}")

    base_url = _require_http_url(body.get("base_url") or "", "base_url")

    timezone = (body.get("timezone") or "UTC").strip() or "UTC"
    schedule_cron = body.get("schedule_cron")
    if schedule_cron is not None and str(schedule_cron).strip():
        schedule_cron = str(schedule_cron).strip()
        _validate_cron(schedule_cron, timezone)
    else:
        schedule_cron = None

    steps = _validate_steps(body.get("steps"), base_url, vendor_ids)

    return {
        "name": name,
        "description": body.get("description") or None,
        "device": device,
        "base_url": base_url,
        "steps": steps,
        "schedule_cron": schedule_cron,
        "timezone": timezone,
        "notify": _normalize_notify(body.get("notify")),
        "groups": _normalize_groups(body.get("groups")),
        "enabled": bool(body.get("enabled", False)),
    }


def _apply_flow_body(flow: TestFlow, body: dict, vendor_ids: set[str]) -> None:
    """Partial-update a TestFlow row from a PUT payload."""
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            raise _ValidationError("name cannot be empty")
        flow.name = name
    if "description" in body:
        flow.description = body.get("description") or None
    if "device" in body:
        device = (body.get("device") or "desktop").strip()
        if device not in _VALID_DEVICE:
            raise _ValidationError(f"device must be one of {sorted(_VALID_DEVICE)}")
        flow.device = device
    if "base_url" in body:
        flow.base_url = _require_http_url(body.get("base_url") or "", "base_url")
    if "timezone" in body:
        flow.timezone = (body.get("timezone") or "UTC").strip() or "UTC"
    if "schedule_cron" in body:
        sc = body.get("schedule_cron")
        if sc is not None and str(sc).strip():
            sc = str(sc).strip()
            _validate_cron(sc, flow.timezone or "UTC")
            flow.schedule_cron = sc
        else:
            flow.schedule_cron = None
    if "steps" in body:
        flow.steps = _validate_steps(body.get("steps"), flow.base_url, vendor_ids)
    if "notify" in body:
        flow.notify = _normalize_notify(body.get("notify"))
    if "groups" in body:
        flow.groups = _normalize_groups(body.get("groups"))
    if "enabled" in body:
        flow.enabled = bool(body.get("enabled"))


def _sync_and_stamp(flow: TestFlow) -> None:
    """Upsert/remove the APScheduler job for this flow and refresh next_run_at."""
    try:
        from app.scheduling.service import compute_flow_next_run, sync_flow_job

        sync_flow_job(flow)
        flow.next_run_at = compute_flow_next_run(flow) if (flow.enabled and flow.schedule_cron) else None
    except Exception:
        logger.warning("failed to sync scheduler job for flow %s", flow.id, exc_info=True)
