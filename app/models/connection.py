import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MCPClient(Base):
    """Registered MCP clients (e.g. Claude.ai). Pre-seeded on deploy."""

    __tablename__ = "mcp_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    allowed_scopes: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OAuthConnection(Base):
    """
    OAuth connection — one per (user, project, provider, external account).

    ``user_id`` is the **token owner** — the person whose Google/Meta/etc.
    account was used to authenticate, and who owns *this* credential. The
    same external account (same ``provider`` + ``google_email``) connected by
    two different users in the same project is stored as two separate rows,
    each with its own tokens (see ``uq_user_project_provider_email``).

    Resolution is owner-aware: ``build_user_context`` loads a user's own
    connections (``user_id``), while ``build_project_context`` loads every
    connection in the project (``project_id``).
    """

    __tablename__ = "oauth_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Project that owns this connection
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,  # nullable during migration backfill, enforced NOT NULL after
        index=True,
    )

    # Token owner — the user who authenticated with the provider
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="google", index=True)
    google_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    connection_status: Mapped[str] = mapped_column(String(50), default="active", index=True)

    __table_args__ = (
        # Per-user: the same external account (provider + google_email) can be
        # connected by multiple users within one project, each keeping their
        # own credentials. user_id is part of the key so those rows coexist.
        UniqueConstraint(
            "user_id", "project_id", "provider", "google_email", name="uq_user_project_provider_email"
        ),
        # Hot-path filters: `_load_connections_and_resources` scans by
        # (project_id|user_id, provider, is_active). Composite indexes let
        # Postgres serve these without touching the base table.
        Index("ix_oauth_project_active_provider", "project_id", "is_active", "provider"),
        Index("ix_oauth_user_active_provider", "user_id", "is_active", "provider"),
    )

    def __repr__(self) -> str:
        return f"<OAuthConnection(project_id={self.project_id}, provider={self.provider}, is_active={self.is_active})>"
