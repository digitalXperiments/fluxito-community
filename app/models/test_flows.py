"""
Tag Testing — Automated Test Flows (SQLAlchemy Models)
========================================================

ORM models for the three tables created in migration 076_test_flows:

  AuditVendor   — A named vendor/beacon endpoint whose network requests a
                  flow can assert on (matched by ``url_pattern`` substring).
  TestFlow      — A saved, replayable browser flow: an ordered list of
                  steps (navigate/click/type/wait) each carrying optional
                  dataLayer + vendor-request assertions.
  TestFlowRun   — One execution of a TestFlow (manual or scheduled).

Follows the same pattern as app/models/auditing.py.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
# AuditVendor
# ---------------------------------------------------------------------------


class AuditVendor(Base):
    """A vendor/beacon endpoint that a test flow can assert network hits on.

    ``url_pattern`` is substring-matched against captured request URLs during
    a flow run. ``params`` describes the extractable parameters for the vendor
    (used by the UI to help authors build assertions):

        [
          {
            "label": "Event name",
            "key": "en",
            "source": "query" | "auto",
            "default": "...",     # optional
            "hint": "..."         # optional
          },
          ...
        ]
    """

    __tablename__ = "audit_vendors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    url_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    catalog_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_audit_vendors_project_slug"),)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "name": self.name,
            "slug": self.slug,
            "url_pattern": self.url_pattern,
            "description": self.description,
            "params": self.params or [],
            "catalog_slug": self.catalog_slug,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# TestFlow
# ---------------------------------------------------------------------------


class TestFlow(Base):
    """A saved, replayable browser flow with per-step assertions.

    ``steps`` JSONB schema::

        [
          {
            "action": "navigate" | "click" | "type" | "wait",
            "label": "Human-readable step name",
            "url": "...",          # navigate (optional; joined to base_url)
            "selector": "...",     # click / type
            "text": "...",         # type
            "ms": 1000,            # wait (capped at 30_000)
            "assertions": {
              "datalayer_events": [
                {
                  "event": "purchase",
                  "mode": "must" | "must_not",
                  "when": "anytime" | "at_step",
                  "fields": [
                    {"key": "value", "op": "equals", "value": "9.99"},
                    ...
                  ]
                }
              ],
              "vendor_requests": [
                {
                  "vendor_id": "<uuid>",
                  "when": "anytime" | "at_step",
                  "mode": "must" | "must_not",   # optional, default must
                  "params": [
                    {"key": "en", "op": "equals", "value": "purchase"},
                    ...
                  ]
                }
              ]
            }
          },
          ...
        ]

    Field/param ops: equals, contains, regex, exists, not_empty. When a check
    has no ``value``, it defaults to ``exists``.
    """

    __tablename__ = "test_flows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    device: Mapped[str] = mapped_column(String(16), nullable=False, server_default="desktop")
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    schedule_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    notify: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    groups: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="never_run")
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list["TestFlowRun"]] = relationship(
        "TestFlowRun",
        back_populates="flow",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint("device IN ('desktop','mobile_web')", name="ck_test_flows_device"),
        CheckConstraint(
            "last_status IN ('passing','failing','error','never_run')",
            name="ck_test_flows_last_status",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "name": self.name,
            "description": self.description,
            "device": self.device,
            "base_url": self.base_url,
            "steps": self.steps or [],
            "schedule_cron": self.schedule_cron,
            "timezone": self.timezone,
            "notify": self.notify or {},
            "groups": self.groups or [],
            "enabled": self.enabled,
            "last_status": self.last_status,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# TestFlowRun
# ---------------------------------------------------------------------------


class TestFlowRun(Base):
    """One execution of a TestFlow."""

    __tablename__ = "test_flow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    assertions_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    assertions_passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    step_results: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audit_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    flow: Mapped["TestFlow"] = relationship("TestFlow", back_populates="runs")

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','passing','failing','error')",
            name="ck_test_flow_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('manual','schedule')",
            name="ck_test_flow_runs_trigger",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "flow_id": str(self.flow_id),
            "project_id": str(self.project_id),
            "status": self.status,
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "assertions_total": self.assertions_total,
            "assertions_passed": self.assertions_passed,
            "step_results": self.step_results,
            "error": self.error,
            "audit_run_id": str(self.audit_run_id) if self.audit_run_id else None,
        }
