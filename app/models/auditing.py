"""
Auditing Platform — SQLAlchemy Models
=======================================

ORM models for the four tables created in migration 052_auditing_platform:

  AuditRun        — One row per audit execution (tag_audit, live_tag_test, etc.)
  AuditFinding    — Individual per-param / per-rule findings for a run
  TagCustomRule   — Project-specific custom audit rules
  LttTestPlan     — Saved live tag test plans

plus AuditFindingTriage (migration 079_audit_finding_triage) — per-project
resolve / snooze state for findings, keyed by a stable finding fingerprint so
it carries across runs (see the class docstring).

Follows the same pattern as app/models/sdr.py.
"""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

# ---------------------------------------------------------------------------
# AuditRun
# ---------------------------------------------------------------------------


class AuditRun(Base):
    """One row per audit run (tag audit, live tag test, etc.)."""

    __tablename__ = "audit_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    audit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    critical_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    info_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="complete")
    triggered_by: Mapped[str] = mapped_column(String(16), nullable=False, server_default="claude")
    url_tested: Mapped[str | None] = mapped_column(Text, nullable=True)
    ltt_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sdr_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationship to findings
    findings: Mapped[list["AuditFinding"]] = relationship(
        "AuditFinding",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint(
            "audit_type IN ('tag_audit','live_tag_test','data_quality','sdr_compliance',"
            "'platform_health','seo','warehouse','full_suite')",
            name="ck_audit_runs_type",
        ),
        CheckConstraint("status IN ('running','complete','error')", name="ck_audit_runs_status"),
        CheckConstraint(
            "triggered_by IN ('claude','schedule','manual')",
            name="ck_audit_runs_triggered_by",
        ),
    )

    def to_dict(self, include_findings: bool = False) -> dict:
        d = {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "audit_type": self.audit_type,
            "title": self.title,
            "score": self.score,
            "critical": self.critical_count,
            "warning": self.warning_count,
            "info": self.info_count,
            "passed": self.passed_count,
            "status": self.status,
            "triggered_by": self.triggered_by,
            "url_tested": self.url_tested,
            "ltt_session_id": self.ltt_session_id,
            "raw_summary": self.raw_summary,
            "created_by": str(self.created_by),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "duration_ms": self.duration_ms,
        }
        if include_findings and self.findings:
            d["findings"] = [f.to_dict() for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# AuditFinding
# ---------------------------------------------------------------------------


class AuditFinding(Base):
    """One row per individual finding in an audit run."""

    __tablename__ = "audit_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audit_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    expected: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    actual: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, server_default="rule_book")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    run: Mapped["AuditRun"] = relationship("AuditRun", back_populates="findings")

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "platform": self.platform,
            "severity": self.severity,
            "rule_id": self.rule_id,
            "event": self.event,
            "passed": self.passed,
            "message": self.message,
            "remediation": self.remediation,
            "expected": self.expected,
            "actual": self.actual,
            "source": self.source,
            "domain": self.domain,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "fingerprint": self.fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        """Stable identity of this finding across runs (see finding_fingerprint)."""
        return finding_fingerprint(
            domain=self.domain,
            platform=self.platform,
            rule_id=self.rule_id,
            event=self.event,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            message=self.message,
        )


def finding_fingerprint(
    *,
    domain: str | None = None,
    platform: str | None = None,
    rule_id: str | None = None,
    event: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    message: str | None = None,
) -> str:
    """Return a stable 40-char fingerprint identifying "the same issue" across runs.

    Every audit run writes fresh ``audit_findings`` rows, so a finding's UUID
    only lives for one run. Triage (resolve / snooze) must survive into later
    runs, so it is keyed on what the finding *is*: domain + platform + rule +
    event + entity. The free-text message is only mixed in when there is no
    rule_id (AI-authored ad-hoc findings), because rule-book messages often
    embed volatile values (counts, sample payloads) that change between runs.
    """
    parts = [domain, platform, rule_id, event, entity_type, entity_id]
    if not rule_id:
        parts.append(" ".join((message or "").lower().split()))
    raw = "\x1f".join((p or "").strip() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AuditFindingTriage
# ---------------------------------------------------------------------------

TRIAGE_OPEN = "open"
TRIAGE_RESOLVED = "resolved"
TRIAGE_SNOOZED = "snoozed"
TRIAGE_STATUSES = (TRIAGE_OPEN, TRIAGE_RESOLVED, TRIAGE_SNOOZED)


def aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AuditFindingTriage(Base):
    """Per-project triage state (open / resolved / snoozed) for one finding.

    Design: findings are rows in ``audit_findings`` but each run inserts new
    rows, so triage is NOT stored on the finding row. It lives here, keyed by
    ``(project_id, fingerprint)`` (see :func:`finding_fingerprint`), and is
    joined onto findings at read time. Consequences:

    * ``resolved`` hides the issue in every run (past and future) until
      someone reopens it.
    * ``snoozed`` with ``snoozed_until`` (7 / 30 days) hides it until that
      moment; afterwards it shows as open again if later runs still find it.
    * ``snoozed`` with ``snoozed_run_id`` ("until next run") hides it in that
      run and any older run, and re-surfaces it in the first newer run that
      still reports it.
    * ``open`` (after "Reopen") is kept as a row for the audit trail.

    Run scores/counts stored on ``audit_runs`` are never rewritten; the UI
    derives open-only counts on the fly.
    """

    __tablename__ = "audit_finding_triage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=TRIAGE_OPEN)
    rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_findings.id", ondelete="SET NULL"), nullable=True
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_runs.id", ondelete="SET NULL"), nullable=True
    )
    # created_at of snoozed_run_id, copied so "until next run" survives the
    # run being deleted and needs no join to evaluate.
    snoozed_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "fingerprint", name="uq_audit_finding_triage_project_fp"),
        CheckConstraint("status IN ('open','resolved','snoozed')", name="ck_audit_finding_triage_status"),
    )

    def effective_status(self, run_created_at: datetime | None = None, now: datetime | None = None) -> str:
        """Status as it applies when viewing a run created at ``run_created_at``.

        Expired snoozes (time passed, or a newer run than the snoozed one) read
        as ``open``. Without ``run_created_at`` a run-scoped snooze is treated
        as active (i.e. "the latest run we know of").
        """
        if self.status == TRIAGE_RESOLVED:
            return TRIAGE_RESOLVED
        if self.status != TRIAGE_SNOOZED:
            return TRIAGE_OPEN
        now = aware_utc(now) or datetime.now(UTC)
        until = aware_utc(self.snoozed_until)
        if until is not None:
            return TRIAGE_SNOOZED if now < until else TRIAGE_OPEN
        run_at = aware_utc(self.snoozed_run_at)
        if run_at is None:
            return TRIAGE_OPEN
        viewed = aware_utc(run_created_at)
        if viewed is None or viewed <= run_at:
            return TRIAGE_SNOOZED
        return TRIAGE_OPEN

    def to_dict(self, run_created_at: datetime | None = None, now: datetime | None = None) -> dict:
        return {
            "status": self.effective_status(run_created_at, now),
            "stored_status": self.status,
            "snoozed_until": self.snoozed_until.isoformat() if self.snoozed_until else None,
            "snooze_until_next_run": self.status == TRIAGE_SNOOZED and self.snoozed_until is None,
            "note": self.note,
            "resolved_by": str(self.resolved_by) if self.resolved_by else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# TagCustomRule
# ---------------------------------------------------------------------------


class TagCustomRule(Base):
    """Project-specific custom audit rule."""

    __tablename__ = "tag_custom_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, server_default="*")
    event: Mapped[str] = mapped_column(String(128), nullable=False, server_default="*")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_params: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    forbidden_params: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    param_assertions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="warning")
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "rule_id", name="uq_tag_custom_rules_project_rule"),
        CheckConstraint("severity IN ('critical','warning','info')", name="ck_tag_custom_rules_severity"),
    )


# ---------------------------------------------------------------------------
# LttTestPlan
# ---------------------------------------------------------------------------


class LttTestPlan(Base):
    """Saved live tag test plan."""

    __tablename__ = "ltt_test_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url_patterns: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    interaction_steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    expected_platforms: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
