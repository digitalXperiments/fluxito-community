"""
Save Audit Result — MCP Tool
==============================

Registers the ``save_audit_result`` tool with the MCP server.

This tool is the persistence bridge between Claude's in-chat audit analysis
and the Fluxito UI.  Call it at the END of any audit workflow to save findings
to the database so they appear in /project/:slug/audits.

Actions
-------
  save              — Bulk-insert an AuditRun + AuditFinding[] rows
  get_run           — Fetch a saved run + paginated findings
  list_runs         — Paginated list of runs for the project
  get_score_summary — Latest score per domain (for UI dashboard cards)
  get_score_history — Score trend over last 30d per domain (for UI sparklines)
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import UTC, datetime

import app.app_state as state
from app.tools.shared_helpers import get_current_user

logger = logging.getLogger(__name__)

_VALID_AUDIT_TYPES = {
    "tag_audit",
    "live_tag_test",
    "data_quality",
    "sdr_compliance",
    "platform_health",
    "seo",
    "warehouse",
    "full_suite",
    "tracking_plan_coverage",
}

# get_score_history window query. ``:days`` is multiplied by a fixed 1-day
# interval so the bind sits OUTSIDE any string literal. (The old
# ``INTERVAL ':days days'`` put the placeholder inside a quoted literal, which
# Postgres treats as literal text → parameter-count mismatch at execution.
# stress-test 2026-06-12, FINDINGS S0/SQL.)
_SCORE_HISTORY_SQL = """
    SELECT audit_type, score, created_at
    FROM audit_runs
    WHERE project_id = :pid
      AND status = 'complete'
      AND created_at >= NOW() - (:days * INTERVAL '1 day')
    ORDER BY audit_type, created_at
"""


def _err(error_type: str, message: str, **extra) -> dict:
    out = {"error": True, "error_type": error_type, "message": message}
    out.update(extra)
    return out


def _get_project_ctx():
    try:
        return state.current_project_ctx.get()
    except LookupError:
        return None


async def persist_audit_run(
    *,
    project_id: _uuid.UUID,
    user_id: _uuid.UUID,
    audit_type: str,
    findings: list[dict],
    title: str | None = None,
    score: int | None = None,
    url_tested: str | None = None,
    ltt_session_id: str | None = None,
    raw_summary: str | None = None,
    duration_ms: int | None = None,
    triggered_by: str = "claude",
) -> dict:
    """Insert an AuditRun + its AuditFinding rows; return the save summary.

    Shared by the ``save_audit_result`` tool and the ``run_audit`` auto-save
    (``app.tools.audit_autosave``) so both write identical rows. Raises on DB
    failure — callers decide how to surface it.
    """
    from app.models.auditing import AuditFinding, AuditRun

    critical = sum(1 for f in findings if f.get("severity") == "critical")
    warning = sum(1 for f in findings if f.get("severity") == "warning")
    info = sum(1 for f in findings if f.get("severity") == "info")
    passed = sum(1 for f in findings if f.get("passed") or f.get("severity") == "pass")

    run = AuditRun(
        project_id=project_id,
        audit_type=audit_type,
        title=(
            title
            or f"{audit_type.replace('_', ' ').title()} — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC"
        )[:255],
        score=score,
        critical_count=critical,
        warning_count=warning,
        info_count=info,
        passed_count=passed,
        status="complete",
        triggered_by=triggered_by,
        url_tested=url_tested,
        ltt_session_id=ltt_session_id,
        raw_summary=raw_summary[:50000] if raw_summary else None,
        created_by=user_id,
        duration_ms=duration_ms,
    )

    async with state.db_session_factory() as db:
        db.add(run)
        await db.flush()  # Get the run.id

        for f in findings:
            sev = f.get("severity") or f.get("status")
            if sev == "pass":
                sev = None  # normalize
            db.add(
                AuditFinding(
                    run_id=run.id,
                    project_id=project_id,
                    domain=audit_type,
                    platform=f.get("platform"),
                    severity=sev,
                    rule_id=f.get("rule_id"),
                    event=f.get("event"),
                    entity_type=f.get("entity_type"),
                    entity_id=f.get("entity_id"),
                    entity_label=f.get("entity_label"),
                    passed=bool(f.get("passed") or f.get("status") == "pass"),
                    expected=f.get("expected"),
                    actual=f.get("actual"),
                    message=f.get("message"),
                    remediation=f.get("remediation"),
                    source=f.get("source") or "rule_book",
                )
            )

        await db.commit()

    from app.config import settings

    view_url = f"{settings.APP_BASE_URL}/audits/run/{run.id}"
    return {
        "success": True,
        "audit_run_id": str(run.id),
        "view_url": view_url,
        "audit_type": audit_type,
        "title": run.title,
        "score": score,
        "critical": critical,
        "warning": warning,
        "info": info,
        "passed": passed,
        "findings_saved": len(findings),
        "message": f"Audit saved. View results at: {view_url}",
    }


def register_save_audit_result_tools(mcp_server) -> None:
    @mcp_server.tool("save_audit_result")
    async def save_audit_result(
        action: str = "save",
        # Save params
        audit_type: str | None = None,
        title: str | None = None,
        score: int | None = None,
        findings: list[dict] | None = None,
        url_tested: str | None = None,
        ltt_session_id: str | None = None,
        raw_summary: str | None = None,
        duration_ms: int | None = None,
        # Fetch params
        run_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        audit_type_filter: str | None = None,
        # Score params
        days: int = 30,
    ) -> dict:
        """
        Save audit results to the Fluxito UI and retrieve audit history.

        Always call this at the END of any audit workflow to persist findings.
        Saved results appear in /project/:slug/audits with scores and trends.

        ──────────────────────────────────────────────────────────────────────

        SAVE

          save (default action)
            Save a completed audit run with all findings.
            params:
              audit_type    REQUIRED — one of: tag_audit, live_tag_test,
                            data_quality, sdr_compliance, platform_health,
                            seo, warehouse, full_suite,
                            tracking_plan_coverage
              title         Human-readable title (e.g. "Tag Audit — shop.example.com")
              score         Overall score 0-100
              findings      List of finding dicts. Each finding should have:
                            {severity, message, platform, event, rule_id,
                             remediation, passed, source, expected, actual}
              url_tested    URL that was tested (for live_tag_test)
              ltt_session_id  Session ID from live_tag_test
              raw_summary   Claude's full audit summary text
              duration_ms   How long the audit took in milliseconds

            returns: {audit_run_id, view_url, score, critical, warning, info, passed}

        RETRIEVE

          get_run
            Fetch a saved audit run + its findings.
            params: run_id (required), limit (findings per page), offset
            Each finding carries triage_status: open | resolved | snoozed
            (set by users in the Fluxito UI, carried across runs). Treat
            resolved/snoozed findings as acknowledged — don't re-flag them
            as new issues; open_counts gives the open-only totals.

          list_runs
            List recent audit runs for the project.
            params: limit, offset, audit_type_filter

        SCORES

          get_score_summary
            Latest score per audit type for the project.
            Returns domain cards for the dashboard.

          get_score_history
            Score trend over the last N days per audit type.
            params: days (default 30)

        ──────────────────────────────────────────────────────────────────────

        Example save call:
          save_audit_result(
            action="save",
            audit_type="tag_audit",
            title="Tag Audit — All GTM Tags",
            score=72,
            findings=[
              {"severity": "critical", "platform": "facebook_pixel",
               "event": "Purchase", "message": "content_ids missing",
               "rule_id": "content_ids.presence", "source": "rule_book",
               "remediation": "Add content_ids array to GTM tag parameters"}
            ],
            raw_summary="Full audit of 23 GTM tags across 5 platforms..."
          )
        """
        user = get_current_user()
        if not user:
            return _err("unauthenticated", "No active session.")

        proj = _get_project_ctx()
        if not proj:
            return _err("no_active_project", "No active project. Call set_active_project first.")

        project_id = _uuid.UUID(str(proj.project_id))
        user_id = _uuid.UUID(str(user.user_id))
        action_norm = (action or "save").strip().lower()

        # ── save ─────────────────────────────────────────────────────────────
        if action_norm == "save":
            if not audit_type:
                return _err(
                    "bad_request",
                    f"audit_type is required. Valid values: {', '.join(sorted(_VALID_AUDIT_TYPES))}",
                )
            if audit_type not in _VALID_AUDIT_TYPES:
                return _err(
                    "bad_request",
                    f"Invalid audit_type '{audit_type}'. Valid: {', '.join(sorted(_VALID_AUDIT_TYPES))}",
                )

            try:
                return await persist_audit_run(
                    project_id=project_id,
                    user_id=user_id,
                    audit_type=audit_type,
                    title=title,
                    score=score,
                    findings=findings or [],
                    url_tested=url_tested,
                    ltt_session_id=ltt_session_id,
                    raw_summary=raw_summary,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                logger.error(f"save_audit_result failed: {e}", exc_info=True)
                return _err("db_error", f"Failed to save audit run: {e}")

        # ── get_run ──────────────────────────────────────────────────────────
        if action_norm == "get_run":
            if not run_id:
                return _err("bad_request", "run_id is required for get_run.")
            try:
                rid = _uuid.UUID(run_id)
            except ValueError:
                return _err("bad_request", "run_id must be a valid UUID.")

            from sqlalchemy import select

            from app.models.auditing import AuditFinding, AuditRun

            async with state.db_session_factory() as db:
                run = (
                    await db.execute(
                        select(AuditRun).where(
                            AuditRun.id == rid,
                            AuditRun.project_id == project_id,
                        )
                    )
                ).scalar_one_or_none()

                if not run:
                    return _err("not_found", f"Audit run '{run_id}' not found.")

                findings_q = (
                    select(AuditFinding)
                    .where(AuditFinding.run_id == rid)
                    .order_by(AuditFinding.created_at)
                    .limit(limit)
                    .offset(offset)
                )
                run_findings = (await db.execute(findings_q)).scalars().all()
                finding_dicts = [f.to_dict() for f in run_findings]

                # Triage (resolve / snooze from the UI) so the AI doesn't
                # re-flag acknowledged issues. Best-effort: never fail get_run.
                triage_summary: dict | None = None
                try:
                    from app.services import audit_triage

                    triage_map = await audit_triage.load_triage_map(
                        db, project_id, (f["fingerprint"] for f in finding_dicts if not f["passed"])
                    )
                    audit_triage.annotate_findings(finding_dicts, triage_map, run.created_at)
                    triage_summary = audit_triage.open_counts(finding_dicts)
                except Exception:
                    logger.debug("get_run triage annotation failed", exc_info=True)

            out = {
                **run.to_dict(),
                "findings": finding_dicts,
                "findings_page": {"limit": limit, "offset": offset, "count": len(run_findings)},
            }
            if triage_summary is not None:
                out["open_counts"] = triage_summary
                if triage_summary["hidden"]:
                    out["triage_note"] = (
                        f"{triage_summary['hidden']} finding(s) on this page are resolved or snoozed "
                        "by the user (triage_status). Don't report them as new issues."
                    )
            return out

        # ── list_runs ────────────────────────────────────────────────────────
        if action_norm == "list_runs":
            from sqlalchemy import desc, select

            from app.models.auditing import AuditRun

            async with state.db_session_factory() as db:
                stmt = (
                    select(AuditRun)
                    .where(AuditRun.project_id == project_id)
                    .order_by(desc(AuditRun.created_at))
                    .limit(limit)
                    .offset(offset)
                )
                if audit_type_filter:
                    stmt = stmt.where(AuditRun.audit_type == audit_type_filter)
                runs = (await db.execute(stmt)).scalars().all()

            return {
                "runs": [r.to_dict() for r in runs],
                "count": len(runs),
                "limit": limit,
                "offset": offset,
            }

        # ── get_score_summary ────────────────────────────────────────────────
        if action_norm == "get_score_summary":
            from sqlalchemy import text

            async with state.db_session_factory() as db:
                result = await db.execute(
                    text("""
                        SELECT DISTINCT ON (audit_type)
                            audit_type, score, critical_count, warning_count,
                            info_count, passed_count, created_at
                        FROM audit_runs
                        WHERE project_id = :pid AND status = 'complete'
                        ORDER BY audit_type, created_at DESC
                    """),
                    {"pid": str(project_id)},
                )
                rows = result.mappings().all()

            return {
                "domains": [
                    {
                        "audit_type": r["audit_type"],
                        "score": r["score"],
                        "critical": r["critical_count"],
                        "warning": r["warning_count"],
                        "last_run": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                    for r in rows
                ],
                "project_id": str(project_id),
            }

        # ── get_score_history ────────────────────────────────────────────────
        if action_norm == "get_score_history":
            from sqlalchemy import text

            async with state.db_session_factory() as db:
                result = await db.execute(
                    text(_SCORE_HISTORY_SQL),
                    {"pid": str(project_id), "days": int(days)},
                )
                rows = result.mappings().all()

            # Group by audit_type
            by_type: dict[str, list] = {}
            for r in rows:
                at = r["audit_type"]
                by_type.setdefault(at, []).append(
                    {
                        "score": r["score"],
                        "date": r["created_at"].date().isoformat() if r["created_at"] else None,
                    }
                )

            return {
                "history": [{"audit_type": k, "data_points": v} for k, v in by_type.items()],
                "days": days,
            }

        return _err(
            "bad_request",
            f"Unknown action '{action}'. Valid: save, get_run, list_runs, "
            "get_score_summary, get_score_history.",
        )
