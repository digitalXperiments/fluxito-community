"""
Test-flow run orchestration.
============================

Single public entry point::

    run_id = await run_flow(flow_id, trigger="manual")   # -> TestFlowRun.id

Pipeline for one run:

  1. Load the flow + its project's vendors.
  2. Insert a ``TestFlowRun`` at status=running.
  3. Execute the flow in headless Chromium (:mod:`.executor`).
  4. Evaluate assertions (:mod:`.assertions`).
  5. Persist step_results / counts / status, mirror onto the flow row.
  6. Mirror an ``AuditRun`` (+ ``AuditFinding`` rows) into the auditing
     history so the run shows up alongside other audits.
  7. Fire notifications on failing/error OR on recovery to passing.

Concurrency is bounded to two simultaneous flow executions process-wide.
Sessions come from ``app_state.db_session_factory`` (same pattern as the
scheduled-report worker).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from html import escape as _html_escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.app_state as app_state
from app.models.auditing import AuditFinding, AuditRun
from app.models.notification_channel import ProjectEmailSender, ProjectSlackWebhook
from app.models.test_flows import AuditVendor, TestFlow, TestFlowRun
from app.tag_testing.flow_runner.assertions import evaluate
from app.tag_testing.flow_runner.executor import execute_flow

logger = logging.getLogger(__name__)

# Bound concurrent browser sessions across the whole process.
_RUN_SEMAPHORE = asyncio.Semaphore(2)


async def run_flow(flow_id: uuid.UUID | str, trigger: str = "manual") -> uuid.UUID:
    """Run one test flow end-to-end. Returns the ``TestFlowRun.id``.

    Raises:
      RuntimeError — if ``app_state.db_session_factory`` isn't wired up.
      LookupError  — if the flow no longer exists.
    """
    sess_factory = app_state.db_session_factory
    if sess_factory is None:
        raise RuntimeError("run_flow called before app_state.db_session_factory was initialised")

    if isinstance(flow_id, str):
        flow_id = uuid.UUID(flow_id)
    if trigger not in ("manual", "schedule"):
        trigger = "manual"

    async with _RUN_SEMAPHORE, sess_factory() as db:
        flow = await db.get(TestFlow, flow_id)
        if flow is None:
            raise LookupError(f"test flow {flow_id} not found")

        # A scheduled fire for a flow that has since been disabled must not run
        # (mirrors run_scheduled_report). A stale/misfired APScheduler job — or
        # a toggle-off that never reached the shared jobstore — can otherwise
        # still execute a disabled flow end-to-end.
        if trigger == "schedule" and not flow.enabled:
            raise LookupError(f"test flow {flow_id} is disabled")

        prev_status = flow.last_status

        vendors = list(
            (await db.execute(select(AuditVendor).where(AuditVendor.project_id == flow.project_id)))
            .scalars()
            .all()
        )

        run = TestFlowRun(
            flow_id=flow.id,
            project_id=flow.project_id,
            status="running",
            trigger=trigger,
            started_at=datetime.utcnow(),
        )
        db.add(run)
        await db.flush()
        run_id: uuid.UUID = run.id

        # ── Execute ──────────────────────────────────────────────────────────
        exec_result = await execute_flow(flow, vendors)

        # ── Evaluate ─────────────────────────────────────────────────────────
        # Run in a worker thread: assertion evaluation runs author-supplied
        # regex patterns via re.search, and a pathological pattern (catastrophic
        # backtracking) would otherwise block the whole event loop.
        evaluation = await asyncio.to_thread(
            evaluate,
            flow.steps or [],
            {
                "datalayer_events": exec_result.datalayer_events,
                "beacons": exec_result.beacons,
            },
        )
        total = evaluation["total"]
        passed = evaluation["passed"]
        any_step_error = any(not s.get("ok", True) for s in (exec_result.step_results or []))

        # ── Status ───────────────────────────────────────────────────────────
        if not exec_result.ok or any_step_error:
            status = "error"
        elif total == passed:
            status = "passing"
        else:
            status = "failing"

        # Merge assertion results back into each step's record.
        merged_steps = _merge_step_results(exec_result.step_results or [], evaluation["per_step"])

        run.status = status
        run.finished_at = datetime.utcnow()
        run.assertions_total = total
        run.assertions_passed = passed
        run.step_results = merged_steps
        run.error = exec_result.error

        flow.last_status = status
        flow.last_run_at = run.finished_at

        # ── Mirror into auditing history ──────────────────────────────────────
        try:
            audit_run = await _mirror_audit_run(
                db, flow, run, status, total, passed, evaluation, vendors, exec_result
            )
            run.audit_run_id = audit_run.id
        except Exception:
            logger.exception("failed to mirror AuditRun for flow run %s", run_id)

        await db.commit()

        # ── Notifications (never fail the run) ────────────────────────────────
        try:
            await _maybe_notify(sess_factory, flow, run_id, status, prev_status, total, passed)
        except Exception:
            logger.exception("notification dispatch failed for flow run %s", run_id)

        return run_id


def _merge_step_results(exec_steps: list[dict], eval_steps: list[dict]) -> list[dict]:
    """Attach per-step assertion outcomes to the executor's step records."""
    eval_by_idx = {s["step_index"]: s.get("results", []) for s in eval_steps}
    out = []
    for s in exec_steps:
        idx = s.get("step_index")
        merged = dict(s)
        merged["assertion_results"] = eval_by_idx.get(idx, [])
        out.append(merged)
    return out


async def _mirror_audit_run(
    db: AsyncSession,
    flow: TestFlow,
    run: TestFlowRun,
    status: str,
    total: int,
    passed: int,
    evaluation: dict,
    vendors: list[AuditVendor],
    exec_result,
) -> AuditRun:
    """Create an AuditRun (+ findings) mirroring this flow run."""
    vendor_by_id = {str(v.id): v for v in vendors}
    failed = total - passed
    score = int(round((passed / total) * 100)) if total else (0 if status == "error" else 100)

    audit_run = AuditRun(
        project_id=flow.project_id,
        audit_type="test_flow",
        title=flow.name,
        score=score,
        warning_count=failed,
        passed_count=passed,
        status="error" if status == "error" else "complete",
        triggered_by="schedule" if run.trigger == "schedule" else "manual",
        url_tested=flow.base_url,
        created_by=flow.created_by or flow.project_id,
    )
    if exec_result.error:
        audit_run.raw_summary = exec_result.error[:4000]
    db.add(audit_run)
    await db.flush()

    for step in evaluation["per_step"]:
        for r in step.get("results", []):
            kind = r.get("kind")
            expected = r.get("expected") or {}
            if kind == "vendor":
                vid = expected.get("vendor_id")
                vendor = vendor_by_id.get(str(vid)) if vid else None
                platform = vendor.slug if vendor else "vendor"
                event = None
            else:
                platform = "datalayer"
                event = expected.get("event")
            db.add(
                AuditFinding(
                    run_id=audit_run.id,
                    project_id=flow.project_id,
                    domain="tag_testing",
                    platform=platform,
                    severity=None if r.get("passed") else "warning",
                    event=event,
                    passed=bool(r.get("passed")),
                    expected=expected,
                    actual=r.get("actual"),
                    message=r.get("description"),
                    source="test_flow",
                )
            )
    return audit_run


async def _maybe_notify(
    sess_factory,
    flow: TestFlow,
    run_id: uuid.UUID,
    status: str,
    prev_status: str,
    total: int,
    passed: int,
) -> None:
    """Notify on failing/error, or on recovery (prev failing/error -> passing)."""
    is_bad = status in ("failing", "error")
    recovered = status == "passing" and prev_status in ("failing", "error")
    if not (is_bad or recovered):
        return

    notify = flow.notify or {}
    webhook_ids = notify.get("slack_webhook_ids") or []
    email_sender_id = notify.get("email_sender_id")
    recipients = [str(x).strip() for x in (notify.get("recipients") or []) if x]
    if not webhook_ids and not (email_sender_id and recipients):
        return

    link = f"/audits/flows/{flow.id}/runs/{run_id}"
    if recovered:
        headline = f"Test flow recovered: {flow.name}"
    else:
        headline = f"Test flow {status}: {flow.name}"
    body = f"{headline}\n{passed}/{total} assertions passed.\n{link}"

    async with sess_factory() as db:
        # Slack.
        for wid in webhook_ids:
            try:
                await _send_slack(db, flow.project_id, wid, headline, body, link)
            except Exception:
                logger.exception("slack notify failed for webhook %s", wid)

        # Email.
        if email_sender_id and recipients:
            try:
                await _send_email(db, flow.project_id, email_sender_id, recipients, headline, body)
            except Exception:
                logger.exception("email notify failed for sender %s", email_sender_id)


async def _send_slack(
    db: AsyncSession,
    project_id: uuid.UUID,
    webhook_id,
    headline: str,
    body: str,
    link: str,
) -> None:
    from app.notifications.slack.base import SlackMessage
    from app.notifications.slack.factory import build_webhook_sender_from_row

    row = await db.get(ProjectSlackWebhook, uuid.UUID(str(webhook_id)))
    if row is None or row.project_id != project_id:
        return
    sender = build_webhook_sender_from_row(row)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{headline}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    await sender.send(SlackMessage(text=headline, blocks=blocks))


async def _send_email(
    db: AsyncSession,
    project_id: uuid.UUID,
    sender_id,
    recipients: list[str],
    headline: str,
    body: str,
) -> None:
    from app.notifications.email.base import EmailMessage
    from app.notifications.email.factory import build_sender_from_row

    row = await db.get(ProjectEmailSender, uuid.UUID(str(sender_id)))
    if row is None or row.project_id != project_id:
        return
    sender = build_sender_from_row(row)
    message = EmailMessage(
        to=tuple(recipients),
        subject=headline,
        text_body=body,
        html_body=f"<p>{_html_escape(headline)}</p><pre>{_html_escape(body)}</pre>",
    )
    await sender.send(message)
