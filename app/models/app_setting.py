"""General application / system settings stored in the database.

This table holds non-bootstrap configuration that used to live only in
environment variables (SMTP, GCS, Sentry, CORS, rate limits, feature flags,
etc.). Values can be marked as secret and are Fernet-encrypted at rest using
the same `TOKEN_ENCRYPTION_KEY` as OAuth app credentials and user tokens.

The service layer (`app/settings_service.py`) is the only place that should
read or write this table directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)

    # Flexible storage. For simple values we usually put them under {"value": ...}
    # For complex objects we store the full object. Secrets are encrypted before
    # being put into this column.
    value_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # When True, the value was encrypted with TOKEN_ENCRYPTION_KEY before storage.
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<AppSetting(key={self.key!r}, is_secret={self.is_secret})>"
