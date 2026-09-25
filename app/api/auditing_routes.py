"""
Auditing Platform — REST API Routes
======================================

Exposes the Auditing Platform (audit_runs + audit_findings + custom rules)
to the Fluxito UI via FastAPI routes.

HTML pages:
  GET /audits                    — List of audit runs (unified dashboard)
  GET /audits/run/{run_id}       — Detail view for a single audit run

JSON API:
  GET  /api/audits               — Paginated list of runs
  GET  /api/audits/{run_id}      — Single run with findings
  POST /api/audits/{run_id}/export — Export findings as JSON/CSV
  POST /api/audits/findings/{finding_id}/resolve — Mark a finding resolved
  POST /api/audits/findings/{finding_id}/snooze  — Snooze (7d / 30d / next_run)
  POST /api/audits/findings/{finding_id}/reopen  — Clear resolve / snooze
  GET  /api/audits/score-summary — Latest score per domain (dashboard cards)
  GET  /api/audits/score-history — Score trend sparklines
  GET  /api/custom-rules         — List custom tag rules for project
  POST /api/custom-rules         — Create/update a custom rule
  DELETE /api/custom-rules/{rule_id} — Soft-delete a custom rule
  GET  /api/tag-rulebook/platforms — List all Rule Book platforms (public docs)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import case, desc, func, select

import app.app_state as app_state
from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx
from app.models.auditing import AuditFinding, AuditRun
from app.services import audit_triage
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

_AUDIT_TYPE_LABELS = {
    "tag_audit": "Tag Audit",
    "live_tag_test": "Live Tag Test",
    "data_quality": "Data Quality",
    "sdr_compliance": "SDR Compliance",
    "platform_health": "Platform Health",
    "seo": "SEO Audit",
    "warehouse": "Warehouse Audit",
    "full_suite": "Full Suite",
}

_SCORE_TIERS = {
    "excellent": (90, 100),
    "good": (70, 89),
    "fair": (50, 69),
    "poor": (0, 49),
}


def _score_tier(score: int | None) -> str:
    if score is None:
        return "unknown"
    for tier, (lo, hi) in _SCORE_TIERS.items():
        if lo <= score <= hi:
            return tier
    return "poor"


# ---------------------------------------------------------------------------
# Auth helpers
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


async def _explicit_project(user_uuid: uuid.UUID, project_id: str | None, fallback: uuid.UUID | None):
    """An explicitly requested project, only if the caller is an active member of it."""
    if not project_id:
        return fallback
    from app.auth.connection_gate import member_role

    if await member_role(str(user_uuid), project_id) is None:
        return None
    return uuid.UUID(str(project_id))


async def _resolve_project_id(request: Request, user_uuid: uuid.UUID) -> uuid.UUID | None:
    """Active project for the caller: request state / ``?project_id=`` (membership
    checked), else the validated active-project cookie."""
    pid_str = request.state.__dict__.get("active_project_id") or request.query_params.get("project_id")
    if pid_str:
        from app.auth.connection_gate import member_role

        try:
            if await member_role(str(user_uuid), str(pid_str)) is not None:
                return uuid.UUID(str(pid_str))
        except ValueError:
            pass
    # Nothing sets request.state.active_project_id, so fall back to the
    # active-project cookie (validated for membership) like the test-flow pages.
    from app.api.project_routes import ensure_active_project

    try:
        cookie_pid = await ensure_active_project(request, str(user_uuid))
        return uuid.UUID(cookie_pid) if cookie_pid else None
    except Exception:
        logger.debug("active project fallback failed", exc_info=True)
        return None


async def _can_triage(user_id: str, project_id: uuid.UUID) -> bool:
    """True when the user may resolve/snooze findings (analysis:write).

    Same gate as the MCP audit write actions (save_audit_result.save): owners,
    admins and every member of a non-RBAC project pass; with RBAC on, members
    need a role granting analysis:write. Non-members get deny-all.
    """
    from app.auth import permissions as _perms

    try:
        eff = await _perms.resolve_effective_permissions(user_id, str(project_id))
    except Exception:
        logger.debug("triage permission lookup failed", exc_info=True)
        return False
    return bool(eff.full or "write" in eff.tools.get("analysis", set()))


# ---------------------------------------------------------------------------
# HTML Pages
# ---------------------------------------------------------------------------


@router.get("/audits")
async def audits_page(request: Request):
    """Unified Auditing dashboard — all audit runs grouped by type."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/audits", status_code=302)

    user_view = await _load_user_view(user_ctx)
    user_uuid = uuid.UUID(user_ctx.user_id)
    project_id = await _resolve_project_id(request, user_uuid)

    from app.tag_testing.rule_books.manifest import list_platforms_summary

    platforms_summary = list_platforms_summary()

    runs = []
    score_summary: list[dict] = []
    active_run: dict | None = None
    open_findings: list[dict] = []
    latest_open_counts: dict | None = None
    can_triage = False
    pages_by_run: dict[str, int] = {}
    flow_count: int | None = None
    vendor_count: int | None = None

    if project_id:
        async with app_state.db_session_factory() as db:
            # Latest 50 runs
            stmt = (
                select(AuditRun)
                .where(
                    AuditRun.project_id == project_id,
                    AuditRun.status == "complete",
                )
                .order_by(desc(AuditRun.created_at))
                .limit(50)
            )
            result = await db.execute(stmt)
            runs = result.scalars().all()

            # Score summary per audit type
            from sqlalchemy import text

            ss_result = await db.execute(
                text("""
                    SELECT DISTINCT ON (audit_type)
                        audit_type, score, critical_count, warning_count, created_at
                    FROM audit_runs
                    WHERE project_id = :pid AND status = 'complete'
                    ORDER BY audit_type, created_at DESC
                """),
                {"pid": str(project_id)},
            )
            score_summary = [dict(r) for r in ss_result.mappings().all()]

            # Live run card — best-effort from AuditRun.status='running'. There's
            # no progress-telemetry table/columns yet (progress_pct, found_so_far,
            # checking_current aren't tracked anywhere), so only title/status are
            # real; the template already renders those sub-sections conditionally
            # and falls back gracefully when they're absent.
            # TODO: to fully populate the live-run feed per the design, add a
            # lightweight progress surface (e.g. an `audit_run_progress` JSONB
            # column updated periodically by the running audit process) and wire
            # it into `active_run` here.
            running_stmt = (
                select(AuditRun)
                .where(AuditRun.project_id == project_id, AuditRun.status == "running")
                .order_by(desc(AuditRun.created_at))
                .limit(1)
            )
            running_run = (await db.execute(running_stmt)).scalar_one_or_none()
            if running_run:
                active_run = {"title": running_run.title, "status": running_run.status}

            # Counts for the Audit sub-nav tabs ("Test flows · N", "Vendors & rules · N").
            # Best-effort: the tabs simply omit the count when this fails.
            try:
                counts_row = (
                    (
                        await db.execute(
                            text("""
                                SELECT
                                    (SELECT count(*) FROM test_flows WHERE project_id = :pid) AS flows,
                                    (SELECT count(*) FROM audit_vendors WHERE project_id = :pid) AS vendors
                            """),
                            {"pid": str(project_id)},
                        )
                    )
                    .mappings()
                    .first()
                )
                if counts_row:
                    flow_count = int(counts_row["flows"])
                    vendor_count = int(counts_row["vendors"])
            except Exception:  # cosmetic counts only
                logger.debug("audit tab counts unavailable", exc_info=True)

            # Open findings — failing findings from the most recent run that
            # aren't resolved/snoozed (audit_finding_triage), worst first.
            # All failing rows are loaded so the latest-run counts can be
            # recomputed over open findings only (the stored score stays).
            if runs:
                severity_rank = case(
                    (AuditFinding.severity == "critical", 0),
                    (AuditFinding.severity == "warning", 1),
                    (AuditFinding.severity == "info", 2),
                    else_=3,
                )
                of_stmt = (
                    select(AuditFinding)
                    .where(AuditFinding.run_id == runs[0].id, AuditFinding.passed.is_(False))
                    .order_by(severity_rank, AuditFinding.created_at)
                    .limit(2000)
                )
                of_result = await db.execute(of_stmt)
                failing = [f.to_dict() for f in of_result.scalars().all()]
                triage_map = await audit_triage.load_triage_map(
                    db, project_id, (f["fingerprint"] for f in failing)
                )
                audit_triage.annotate_findings(failing, triage_map, runs[0].created_at)
                latest_open_counts = audit_triage.open_counts(failing)
                open_findings = [f for f in failing if f["triage_status"] == "open"][:50]

                # Pages-tested count per run, derived from findings whose
                # entity_type is 'page' (the convention the save_audit_result
                # tool docs recommend for per-page entities). Runs that don't
                # use that convention simply render "—" in the template.
                run_ids = [r.id for r in runs]
                pg_stmt = (
                    select(AuditFinding.run_id, func.count(func.distinct(AuditFinding.entity_id)))
                    .where(AuditFinding.run_id.in_(run_ids), AuditFinding.entity_type == "page")
                    .group_by(AuditFinding.run_id)
                )
                pg_result = await db.execute(pg_stmt)
                pages_by_run = {str(rid): cnt for rid, cnt in pg_result.all()}

    run_dicts = []
    for r in runs:
        d = r.to_dict()
        d["pages_tested"] = pages_by_run.get(str(r.id))
        run_dicts.append(d)

    if project_id and open_findings:
        can_triage = await _can_triage(user_ctx.user_id, project_id)

    return render(
        request,
        "audits/index.html",
        {
            "user": user_view,
            "runs": run_dicts,
            "score_summary": score_summary,
            "active_run": active_run,
            "open_findings": open_findings,
            "latest_open_counts": latest_open_counts,
            "can_triage": can_triage,
            "platforms_summary": platforms_summary,
            "audit_type_labels": _AUDIT_TYPE_LABELS,
            "audit_flow_count": flow_count,
            "audit_vendor_count": vendor_count,
            "page_title": "Auditing — Fluxito",
            "active_nav": "audits",
        },
    )


@router.get("/audits/run/{run_id}")
async def audit_run_detail_page(request: Request, run_id: str):
    """Detail page for a single audit run."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(f"/signin?next=/audits/run/{run_id}", status_code=302)

    user_view = await _load_user_view(user_ctx)
    user_uuid = uuid.UUID(user_ctx.user_id)
    project_id = await _resolve_project_id(request, user_uuid)

    if not project_id:
        raise HTTPException(status_code=404, detail="No active project")

    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid run ID")

    async with app_state.db_session_factory() as db:
        run = (
            await db.execute(select(AuditRun).where(AuditRun.id == rid, AuditRun.project_id == project_id))
        ).scalar_one_or_none()

        if not run:
            raise HTTPException(status_code=404, detail="Audit run not found")

        findings = (
            (
                await db.execute(
                    select(AuditFinding)
                    .where(AuditFinding.run_id == rid)
                    .order_by(
                        # Sort: critical first, then warning, then info, then pass
                        AuditFinding.passed.asc(),
                        AuditFinding.severity.desc(),
                        AuditFinding.platform,
                    )
                    .limit(2000)
                )
            )
            .scalars()
            .all()
        )
        finding_dicts = [f.to_dict() for f in findings]
        triage_map = await audit_triage.load_triage_map(
            db, project_id, (f["fingerprint"] for f in finding_dicts if not f["passed"])
        )
    audit_triage.annotate_findings(finding_dicts, triage_map, run.created_at)
    can_triage = await _can_triage(user_ctx.user_id, project_id)

    return render(
        request,
        "audits/run_detail.html",
        {
            "user": user_view,
            "run": run.to_dict(),
            "findings": finding_dicts,
            "open_counts": audit_triage.open_counts(finding_dicts),
            "can_triage": can_triage,
            "score_tier": _score_tier(run.score),
            "audit_type_labels": _AUDIT_TYPE_LABELS,
            "page_title": f"Audit: {run.title} — Fluxito",
            "active_nav": "audits",
        },
    )


# ---------------------------------------------------------------------------
# JSON API — Audit Runs
# ---------------------------------------------------------------------------


@router.get("/api/audits")
async def api_list_audit_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    audit_type: str | None = None,
    project_id: str | None = None,
):
    """List audit runs for the project."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)

    async with app_state.db_session_factory() as db:
        stmt = (
            select(AuditRun)
            .where(AuditRun.project_id == pid, AuditRun.status == "complete")
            .order_by(desc(AuditRun.created_at))
            .limit(limit)
            .offset(offset)
        )
        if audit_type:
            stmt = stmt.where(AuditRun.audit_type == audit_type)
        runs = (await db.execute(stmt)).scalars().all()

    return JSONResponse(
        {
            "runs": [r.to_dict() for r in runs],
            "count": len(runs),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/audits/score-summary")
async def api_score_summary(request: Request, project_id: str | None = None):
    """Latest score per audit type for dashboard cards."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"domains": []})

    from sqlalchemy import text

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            text("""
                SELECT DISTINCT ON (audit_type)
                    audit_type, score, critical_count, warning_count,
                    info_count, passed_count, created_at, id
                FROM audit_runs
                WHERE project_id = :pid AND status = 'complete'
                ORDER BY audit_type, created_at DESC
            """),
            {"pid": str(pid)},
        )
        rows = result.mappings().all()

    return JSONResponse(
        {
            "domains": [
                {
                    "audit_type": r["audit_type"],
                    "label": _AUDIT_TYPE_LABELS.get(r["audit_type"], r["audit_type"]),
                    "score": r["score"],
                    "tier": _score_tier(r["score"]),
                    "critical": r["critical_count"],
                    "warning": r["warning_count"],
                    "last_run": r["created_at"].isoformat() if r["created_at"] else None,
                    "run_id": str(r["id"]),
                }
                for r in rows
            ]
        }
    )


@router.get("/api/audits/score-history")
async def api_score_history(
    request: Request,
    days: int = Query(30, ge=7, le=365),
    audit_type: str | None = None,
    project_id: str | None = None,
):
    """Score trend for sparklines."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"history": []})

    from sqlalchemy import text

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            text(f"""
                SELECT audit_type, score, created_at
                FROM audit_runs
                WHERE project_id = :pid
                  AND status = 'complete'
                  AND created_at >= NOW() - INTERVAL '{int(days)} days'
                ORDER BY audit_type, created_at
            """),
            {"pid": str(pid)},
        )
        rows = result.mappings().all()

    by_type: dict[str, list] = {}
    for r in rows:
        at = r["audit_type"]
        if audit_type and at != audit_type:
            continue
        by_type.setdefault(at, []).append(
            {
                "score": r["score"],
                "date": r["created_at"].date().isoformat() if r["created_at"] else None,
            }
        )

    return JSONResponse(
        {
            "history": [
                {"audit_type": k, "label": _AUDIT_TYPE_LABELS.get(k, k), "data_points": v}
                for k, v in by_type.items()
            ],
            "days": days,
        }
    )


@router.get("/api/audits/{run_id}")
async def api_get_audit_run(
    request: Request,
    run_id: str,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    severity: str | None = None,
):
    """Single audit run with paginated findings."""
    user_ctx, user_uuid = await _require_user(request)
    project_id = await _resolve_project_id(request, user_uuid)
    if not project_id:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)

    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid run ID")

    async with app_state.db_session_factory() as db:
        run = (
            await db.execute(select(AuditRun).where(AuditRun.id == rid, AuditRun.project_id == project_id))
        ).scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Audit run not found")

        stmt = (
            select(AuditFinding)
            .where(AuditFinding.run_id == rid)
            .order_by(AuditFinding.passed.asc(), AuditFinding.created_at)
            .limit(limit)
            .offset(offset)
        )
        if severity:
            stmt = stmt.where(AuditFinding.severity == severity)
        findings = (await db.execute(stmt)).scalars().all()
        finding_dicts = [f.to_dict() for f in findings]
        triage_map = await audit_triage.load_triage_map(
            db, project_id, (f["fingerprint"] for f in finding_dicts if not f["passed"])
        )
    audit_triage.annotate_findings(finding_dicts, triage_map, run.created_at)

    return JSONResponse(
        {
            **run.to_dict(),
            "findings": finding_dicts,
            "findings_page": {"limit": limit, "offset": offset, "count": len(findings)},
        }
    )


@router.post("/api/audits/{run_id}/export")
async def api_export_audit_run(request: Request, run_id: str):
    """Export findings as CSV or JSON."""
    user_ctx, user_uuid = await _require_user(request)
    project_id = await _resolve_project_id(request, user_uuid)
    if not project_id:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)

    body = await request.json()
    fmt = (body.get("format") or "json").lower()

    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid run ID")

    async with app_state.db_session_factory() as db:
        run = (
            await db.execute(select(AuditRun).where(AuditRun.id == rid, AuditRun.project_id == project_id))
        ).scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Audit run not found")
        findings = (
            (
                await db.execute(
                    select(AuditFinding)
                    .where(AuditFinding.run_id == rid)
                    .order_by(AuditFinding.passed.asc(), AuditFinding.created_at)
                )
            )
            .scalars()
            .all()
        )

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "platform",
                "severity",
                "rule_id",
                "event",
                "passed",
                "message",
                "remediation",
                "source",
            ],
        )
        writer.writeheader()
        for f in findings:
            d = f.to_dict()
            writer.writerow({k: d.get(k, "") for k in writer.fieldnames})
        output.seek(0)
        fname = f"audit_{run_id[:8]}.csv"
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # JSON export
    payload = {
        **run.to_dict(),
        "findings": [f.to_dict() for f in findings],
    }
    fname = f"audit_{run_id[:8]}.json"
    return StreamingResponse(
        io.BytesIO(json.dumps(payload, indent=2, default=str).encode()),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# JSON API — Finding triage (resolve / snooze / reopen)
# ---------------------------------------------------------------------------


async def _triage_finding(request: Request, finding_id: str, action: str) -> JSONResponse:
    user_ctx, user_uuid = await _require_user(request)
    project_id = await _resolve_project_id(request, user_uuid)
    if not project_id:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)
    # Membership + analysis:write (non-members resolve to deny-all → 403).
    from app.auth.web_guards import require_domain_permission

    await require_domain_permission(user_ctx.user_id, str(project_id), "analysis", "write")

    try:
        fid = uuid.UUID(finding_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid finding ID")

    body: dict = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                body = parsed
    except ValueError:
        return JSONResponse({"error": True, "message": "Invalid JSON body."}, status_code=400)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return JSONResponse({"error": True, "message": "note must be a string."}, status_code=400)
    snooze = body.get("snooze") or body.get("duration")

    async with app_state.db_session_factory() as db:
        row = (
            await db.execute(
                select(AuditFinding, AuditRun)
                .join(AuditRun, AuditRun.id == AuditFinding.run_id)
                .where(AuditFinding.id == fid, AuditRun.project_id == project_id)
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Finding not found")
        finding, run = row
        try:
            triage = await audit_triage.set_triage(
                db,
                project_id=project_id,
                finding=finding,
                run=run,
                action=action,
                user_id=user_uuid,
                snooze=snooze,
                note=note,
            )
        except audit_triage.TriageError as e:
            return JSONResponse({"error": True, "message": str(e)}, status_code=400)
        out = triage.to_dict(run.created_at)
        fingerprint = finding.fingerprint
        await db.commit()

    return JSONResponse(
        {
            "ok": True,
            "finding_id": str(fid),
            "fingerprint": fingerprint,
            "triage_status": out["status"],
            "triage": out,
        }
    )


@router.post("/api/audits/findings/{finding_id}/resolve")
async def api_resolve_finding(request: Request, finding_id: str):
    """Mark a finding resolved (hidden in this and later runs until reopened)."""
    return await _triage_finding(request, finding_id, "resolve")


@router.post("/api/audits/findings/{finding_id}/snooze")
async def api_snooze_finding(request: Request, finding_id: str):
    """Snooze a finding. Body: {"snooze": "7d" | "30d" | "next_run", "note"?}."""
    return await _triage_finding(request, finding_id, "snooze")


@router.post("/api/audits/findings/{finding_id}/reopen")
async def api_reopen_finding(request: Request, finding_id: str):
    """Clear a resolve / snooze so the finding counts as open again."""
    return await _triage_finding(request, finding_id, "reopen")


# ---------------------------------------------------------------------------
# JSON API — Custom Rules
# ---------------------------------------------------------------------------


@router.get("/api/custom-rules")
async def api_list_custom_rules(request: Request, project_id: str | None = None):
    """List project custom audit rules."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"rules": [], "count": 0})

    from app.tag_testing.rule_books.custom_rules import get_custom_rules

    rules = await get_custom_rules(str(pid))
    return JSONResponse({"rules": rules, "count": len(rules)})


@router.post("/api/custom-rules")
async def api_save_custom_rule(request: Request, project_id: str | None = None):
    """Create or update a custom rule."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)
    if not await _can_triage(str(user_uuid), pid):
        return JSONResponse(
            {"error": True, "message": "Your role doesn't allow changing audit rules."}, status_code=403
        )

    body = await request.json()
    from app.tag_testing.rule_books.custom_rules import save_custom_rule

    result = await save_custom_rule(str(pid), str(user_uuid), body)
    status = 400 if result.get("error") else 200
    return JSONResponse(result, status_code=status)


@router.delete("/api/custom-rules/{rule_id}")
async def api_delete_custom_rule(request: Request, rule_id: str, project_id: str | None = None):
    """Soft-delete a custom rule."""
    user_ctx, user_uuid = await _require_user(request)
    pid = await _resolve_project_id(request, user_uuid)
    pid = await _explicit_project(user_uuid, project_id, pid)
    if not pid:
        return JSONResponse({"error": True, "message": "No active project."}, status_code=400)
    if not await _can_triage(str(user_uuid), pid):
        return JSONResponse(
            {"error": True, "message": "Your role doesn't allow changing audit rules."}, status_code=403
        )

    from app.tag_testing.rule_books.custom_rules import delete_custom_rule

    result = await delete_custom_rule(str(pid), rule_id)
    status = 404 if result.get("error_type") == "not_found" else 200
    return JSONResponse(result, status_code=status)


# ---------------------------------------------------------------------------
# JSON API — Tag Rule Book (public docs)
# ---------------------------------------------------------------------------


@router.get("/api/tag-rulebook/platforms")
async def api_tag_rulebook_platforms(request: Request):
    """List all Rule Book platforms (no auth required — public spec)."""
    from app.tag_testing.rule_books.manifest import list_platforms_summary

    return JSONResponse({"platforms": list_platforms_summary(), "total": len(list_platforms_summary())})


@router.get("/api/tag-rulebook/platforms/{platform}")
async def api_tag_rulebook_platform_spec(request: Request, platform: str):
    """Full spec for a single platform (public)."""
    from app.tag_testing.rule_books.manifest import get_rule_book

    rb = get_rule_book(platform)
    if not rb:
        raise HTTPException(status_code=404, detail=f"No Rule Book for platform '{platform}'")
    return JSONResponse(rb.serialize(include_events=True))
