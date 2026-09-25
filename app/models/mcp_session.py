import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MCPSession(Base):
    """MCP access/refresh tokens (OAuth) or Personal Access Tokens (PATs) for headless/remote clients.
    Validated on every /mcp tool call and /oauth/userinfo etc. via access_token_hash.
    """

    __tablename__ = "mcp_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    access_token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scopes: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # PAT support (kind='pat' for tokens generated in web UI for remote/headless use;
    # 'oauth' for normal client-driven OAuth sessions). Backfilled + defaulted in migration 051.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="oauth")
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("idx_user_active", user_id, is_revoked),)

    def __repr__(self) -> str:
        return f"<MCPSession(user_id={self.user_id}, kind={self.kind}, is_revoked={self.is_revoked})>"
