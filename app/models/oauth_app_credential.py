"""OAuth app credentials — OAuth client IDs/secrets.

``OAuthAppCredential`` is the install-wide app: one row per platform.
``ProjectOAuthAppCredential`` is an optional per-project override (one row
per project + platform) — a project that brings its own developer app
connects and refreshes through it instead of the install's. The
`client_secret` fields store Fernet ciphertext; encryption/decryption is
handled by `app.auth.oauth_app_credentials`, not by the models.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

SUPPORTED_PLATFORMS = (
    "google",
    "meta",
    "tiktok",
    "snap",
    "linkedin",
    "pinterest",
    "x",
    "reddit",
    "bing",
    "apple",
)


class OAuthAppCredential(Base):
    __tablename__ = "oauth_app_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    extra_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "platform IN ('google', 'meta', 'tiktok', 'snap', 'linkedin', 'pinterest', 'x', 'reddit', 'bing', 'apple')",
            name="ck_oauth_app_credentials_platform_valid",
        ),
    )

    def __repr__(self) -> str:
        return f"<OAuthAppCredential(platform={self.platform!r})>"


class ProjectOAuthAppCredential(Base):
    __tablename__ = "project_oauth_app_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    extra_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("project_id", "platform", name="uq_project_oauth_app_platform"),
        CheckConstraint(
            "platform IN ('google', 'meta', 'tiktok', 'snap', 'linkedin', 'pinterest', 'x', 'reddit', 'bing', 'apple')",
            name="ck_project_oauth_app_credentials_platform_valid",
        ),
    )

    def __repr__(self) -> str:
        return f"<ProjectOAuthAppCredential(project_id={self.project_id!s}, platform={self.platform!r})>"
