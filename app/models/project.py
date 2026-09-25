"""Project model — data boundary and collaboration unit.

Every connector, dashboard, audit row, and member belongs to a project.
The owner is the first member; additional members are invited with the
'admin' or 'member' role via ProjectMember.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

# ---------------------------------------------------------------------------
# Free-tier connector allowlist
# ---------------------------------------------------------------------------
FREE_ALLOWED_CONNECTORS = {"ga4", "gtm"}


# ---------------------------------------------------------------------------
# Project roles
# ---------------------------------------------------------------------------
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
VALID_ROLES = {ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER}

# Roles that can connect/disconnect platforms
CAN_CONNECT_ROLES = {ROLE_OWNER, ROLE_ADMIN}

# Roles that can invite/remove members
CAN_MANAGE_MEMBERS_ROLES = {ROLE_OWNER, ROLE_ADMIN}

# Roles that can create/assign custom roles and toggle RBAC
CAN_MANAGE_ROLES = {ROLE_OWNER, ROLE_ADMIN}


# ---------------------------------------------------------------------------
# Project — the data boundary and collaboration space
# ---------------------------------------------------------------------------
class Project(Base):
    """
    A project is the data boundary and collaboration space.

    All connectors, dashboards, KPIs, business context, and audit records
    belong to a project. The owner is the primary contact.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Owner — the primary contact. FK to users.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # --- dashboard style config (consumed by the Haiku style-guide prompt) ---
    dashboard_style_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # When False (default) all members retain full access (pre-RBAC behavior).
    # When True, members are restricted to their assigned roles.
    rbac_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Relationships
    members: Mapped[list["ProjectMember"]] = relationship(
        "ProjectMember",
        back_populates="project",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# ProjectMember — maps users to projects with roles
# ---------------------------------------------------------------------------
class ProjectMember(Base):
    """
    Maps users to projects with a role (owner/admin/member).

    The owner role is singular per project — only one member can be owner.
    Ownership transfer is a two-step operation: demote old owner, promote new.
    """

    __tablename__ = "project_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="member"
    )  # 'owner' | 'admin' | 'member'

    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    project: Mapped["Project"] = relationship("Project", back_populates="members")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_project_member_role",
        ),
    )
