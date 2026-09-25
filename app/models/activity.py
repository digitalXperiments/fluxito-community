"""User activity event log — sign-ins, project creation, member invites, etc.

Distinct from `tool_call_audit` (in app.models.audit), which is per-MCP-tool-call.
The `activity_events` table records higher-level user actions for audit
visibility on the project settings page.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ActivityEvent(Base):
    """
    Non-tool-call events: sign-ins, connections added/removed, permission changes.
    Displayed alongside UsageLedger entries in the activity log.
    """

    __tablename__ = "activity_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Optional project scope (sign-ins are user-level, connection adds are project-level)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # 'signin' | 'connection_added' | 'connection_removed' | 'permission_changed'
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
