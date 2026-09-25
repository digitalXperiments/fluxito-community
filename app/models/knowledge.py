"""
KPI Library and Business Context models.

Both tables are scoped to a **project**. All KPI definitions and business
context documents belong to a project, not to a user or organization.

These tables feed the MCP tools under ``app/tools/knowledge_tools.py`` so
Claude can answer questions using the project's own terminology, formulas,
and business context.

KPI model
---------
A KPI is a structured, executable spec — not a glossary entry. Each KPI
has:

* **Identity** — slug, name, aliases, status (draft/approved/deprecated),
  version.
* **Definition** — description, business question, interpretation guide.
* **Computation** — an ``expression`` referencing input keys (e.g.
  ``"{a} / {b}"``) plus ``inputs`` rows, each bound to a specific
  connector + field. Single-source KPIs compile push-down; cross-source
  KPIs pull up and evaluate in-app.
* **Quality** — unit, format, direction, target, expected range.

Only ``status='approved'`` KPIs with at least one input are considered
MCP-ready; draft / input-less KPIs are hidden from Claude by default.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class KPI(Base):
    """A structured KPI entry in the per-project catalog."""

    __tablename__ = "kpis"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','approved','deprecated')",
            name="ck_kpis_status",
        ),
        CheckConstraint(
            "direction IS NULL OR direction IN ('higher_better','lower_better','neutral')",
            name="ck_kpis_direction",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Project scope
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Identity
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Definition
    description: Mapped[str] = mapped_column(Text, nullable=False)
    business_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    interpretation_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Computation spec — expression references input keys defined by the
    # KPIInput rows. Null until the structured picker is filled in.
    expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_grain: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    format_spec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Quality
    target_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_range_min: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    expected_range_max: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

    # Ownership
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_of_truth_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Audit
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    inputs: Mapped[list["KPIInput"]] = relationship(
        "KPIInput",
        back_populates="kpi",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self, *, include_inputs: bool = True) -> dict:
        data = {
            "id": str(self.id),
            "slug": self.slug,
            "name": self.name,
            "aliases": list(self.aliases or []),
            "status": self.status,
            "version": self.version,
            "description": self.description,
            "business_question": self.business_question,
            "interpretation_guide": self.interpretation_guide,
            "category": self.category,
            "tags": list(self.tags or []),
            "expression": self.expression,
            "time_grain": self.time_grain,
            "unit": self.unit,
            "format_spec": self.format_spec,
            "direction": self.direction,
            "target_value": float(self.target_value) if self.target_value is not None else None,
            "target_type": self.target_type,
            "expected_range_min": (
                float(self.expected_range_min) if self.expected_range_min is not None else None
            ),
            "expected_range_max": (
                float(self.expected_range_max) if self.expected_range_max is not None else None
            ),
            "owner": self.owner,
            "source_of_truth_url": self.source_of_truth_url,
            "last_reviewed_at": self.last_reviewed_at.isoformat() if self.last_reviewed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_inputs:
            data["inputs"] = [i.to_dict() for i in (self.inputs or [])]
        return data


class KPIInput(Base):
    """One bound input for a KPI formula — references a connection and a
    connector-specific field selection."""

    __tablename__ = "kpi_inputs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    kpi_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kpis.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Key referenced inside ``KPI.expression`` — e.g. ``"a"`` / ``"spend"``.
    key: Mapped[str] = mapped_column(String(32), nullable=False)

    # Source platform: 'ga4' | 'bigquery' | 'google_ads' | 'search_console' |
    # 'amplitude' | 'adobe_analytics' | 'meta_ads' | 'tiktok_ads' | 'snap_ads'.
    # Determines which connection table to resolve ``connection_id`` against
    # and which connector to use at execution time.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    # No FK constraint — connection_id may point at oauth_connections,
    # bq_connections, or another credential table depending on ``source``.
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Connector-specific binding payload. Shape validated in the service
    # layer per connector type.
    binding: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    kpi: Mapped["KPI"] = relationship("KPI", back_populates="inputs")

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "key": self.key,
            "source": self.source,
            "connection_id": str(self.connection_id),
            "binding": dict(self.binding or {}),
        }


class BusinessContext(Base):
    """
    One free-form Markdown document per project describing the business —
    industry, audience, competitors, goals, seasonality, etc. Loaded by
    Claude to ground its analytics answers in real context.
    """

    __tablename__ = "business_contexts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
