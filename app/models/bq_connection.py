import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

# Constants for connection status
CONNECTION_STATUS_ACTIVE = "active"
CONNECTION_STATUS_DISCONNECTED = "disconnected"
CONNECTION_STATUS_ERROR = "error"


class BQConnection(Base):
    """BigQuery service-account connection scoped to a project."""

    __tablename__ = "bq_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Fluxito project that owns this connection
    fluxito_project_id: Mapped[uuid.UUID | None] = mapped_column(
        "fluxito_project_id",
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,  # nullable during migration backfill
        index=True,
    )

    # Token owner — the user who provided the service account
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # GCP project ID (not the internal Fluxito project)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    datasets: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    service_account_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    connection_status: Mapped[str] = mapped_column(String(50), default=CONNECTION_STATUS_ACTIVE, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_bq_project_user_active", fluxito_project_id, user_id, is_active),)

    def __repr__(self) -> str:
        return f"<BQConnection(project={self.project_id}, is_active={self.is_active})>"
