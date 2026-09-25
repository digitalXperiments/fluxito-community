"""
ToolCallAudit — per-invocation audit record for MCP tool calls.

Stores the full request/response pair so users can click on an AI answer
and see exactly what data was fetched, with what parameters, when, and by
which AI client (Claude, ChatGPT, Cursor, etc.).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ToolCallAudit(Base):
    __tablename__ = "tool_call_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Project scope (nullable for pre-migration historical records)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The user who made this tool call
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_client: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="success", index=True
    )  # success | error | denied
    is_write: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Exact arguments the AI client passed to the tool
    arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # One-line description of what came back (e.g. "8 campaigns returned")
    response_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Full response body, truncated to ~32KB
    response_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )

    # Max characters we persist for response_preview. Above this, we truncate
    # and set response_truncated=True so the UI can show a "truncated" badge.
    MAX_RESPONSE_CHARS = 32_000

    __table_args__ = (
        # Supports the audit-history UI which filters/sorts by all four.
        Index(
            "ix_audit_user_tool_status_ts",
            "user_id",
            "tool_name",
            "status",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return f"<ToolCallAudit(tool={self.tool_name}, status={self.status})>"

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "tool_name": self.tool_name,
            "platform": self.platform,
            "source_client": self.source_client,
            "status": self.status,
            "is_write": self.is_write,
            "duration_ms": self.duration_ms,
            "arguments": self.arguments or {},
            "response_summary": self.response_summary,
            "response_preview": self.response_preview,
            "response_truncated": self.response_truncated,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
