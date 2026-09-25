"""
Audit finding triage — resolve / snooze / reopen.
===================================================

Shared by the web routes (app/api/auditing_routes.py) and the MCP
``save_audit_result`` tool so both agree on which findings are "open".

Triage lives in ``audit_finding_triage`` keyed by (project_id, fingerprint);
see :class:`app.models.auditing.AuditFindingTriage` for the semantics.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auditing import (
    TRIAGE_OPEN,
    TRIAGE_RESOLVED,
    TRIAGE_SNOOZED,
    AuditFinding,
    AuditFindingTriage,
    AuditRun,
    aware_utc,
)

# Snooze options offered by the UI / accepted by the API.
SNOOZE_DAYS = {"7d": 7, "30d": 30}
SNOOZE_NEXT_RUN = "next_run"
SNOOZE_OPTIONS = (*SNOOZE_DAYS, SNOOZE_NEXT_RUN)

_NOTE_MAX = 2000


async def load_triage_map(
    db: AsyncSession, project_id: uuid.UUID, fingerprints: Iterable[str] | None = None
) -> dict[str, AuditFindingTriage]:
    """Return {fingerprint: triage row} for the project (optionally filtered)."""
    stmt = select(AuditFindingTriage).where(AuditFindingTriage.project_id == project_id)
    if fingerprints is not None:
        fps = sorted(set(fingerprints))
        if not fps:
            return {}
        stmt = stmt.where(AuditFindingTriage.fingerprint.in_(fps))
    rows = (await db.execute(stmt)).scalars().all()
    return {r.fingerprint: r for r in rows}


def annotate_findings(
    findings: list[dict],
    triage_map: dict[str, AuditFindingTriage],
    run_created_at: datetime | None,
    now: datetime | None = None,
) -> list[dict]:
    """Add ``triage_status`` (+ ``triage`` details) to finding dicts in place.

    Passed checks are always ``open`` (nothing to triage). Returns the list.
    """
    now = now or datetime.now(UTC)
    for f in findings:
        row = None if f.get("passed") else triage_map.get(f.get("fingerprint") or "")
        if row is None:
            f["triage_status"] = TRIAGE_OPEN
            f["triage"] = None
        else:
            d = row.to_dict(run_created_at, now)
            f["triage_status"] = d["status"]
            f["triage"] = d
    return findings


def open_counts(findings: list[dict]) -> dict:
    """Severity counts over failing findings that are currently open."""
    out = {"critical": 0, "warning": 0, "info": 0, "open": 0, "resolved": 0, "snoozed": 0}
    for f in findings:
        if f.get("passed"):
            continue
        st = f.get("triage_status") or TRIAGE_OPEN
        if st != TRIAGE_OPEN:
            out[st] = out.get(st, 0) + 1
            continue
        out["open"] += 1
        sev = f.get("severity")
        if sev in ("critical", "warning"):
            out[sev] += 1
        else:
            out["info"] += 1
    out["hidden"] = out["resolved"] + out["snoozed"]
    return out


class TriageError(ValueError):
    """Invalid triage request (bad action / snooze option)."""


async def set_triage(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    finding: AuditFinding,
    run: AuditRun,
    action: str,
    user_id: uuid.UUID,
    snooze: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> AuditFindingTriage:
    """Upsert the triage row for ``finding`` and return it (caller commits).

    action: ``resolve`` | ``snooze`` | ``reopen``.
    snooze: ``7d`` | ``30d`` | ``next_run`` (required for ``snooze``).
    """
    now = now or datetime.now(UTC)
    if action not in ("resolve", "snooze", "reopen"):
        raise TriageError(f"Unknown action '{action}'.")
    if action == "snooze" and snooze not in SNOOZE_OPTIONS:
        raise TriageError(f"snooze must be one of: {', '.join(SNOOZE_OPTIONS)}.")
    if finding.passed:
        raise TriageError("Passed checks can't be triaged.")

    fp = finding.fingerprint
    row = (
        await db.execute(
            select(AuditFindingTriage).where(
                AuditFindingTriage.project_id == project_id,
                AuditFindingTriage.fingerprint == fp,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AuditFindingTriage(project_id=project_id, fingerprint=fp)
        db.add(row)

    row.rule_id = finding.rule_id
    row.finding_id = finding.id
    row.updated_by = user_id
    row.updated_at = now
    if note is not None:
        row.note = note.strip()[:_NOTE_MAX] or None

    row.snoozed_until = None
    row.snoozed_run_id = None
    row.snoozed_run_at = None
    if action == "resolve":
        row.status = TRIAGE_RESOLVED
        row.resolved_by = user_id
        row.resolved_at = now
    elif action == "snooze":
        row.status = TRIAGE_SNOOZED
        row.resolved_by = None
        row.resolved_at = None
        if snooze in SNOOZE_DAYS:
            row.snoozed_until = now + timedelta(days=SNOOZE_DAYS[snooze])
        else:
            row.snoozed_run_id = run.id
            row.snoozed_run_at = aware_utc(run.created_at) or now
    else:  # reopen
        row.status = TRIAGE_OPEN
        row.resolved_by = None
        row.resolved_at = None
    await db.flush()
    return row
