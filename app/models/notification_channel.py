"""
Notification channel models: per-project email senders and Slack webhooks.

  * ``ProjectEmailSender``   — BYO SMTP / SES credentials, per project. The
                               ``config_encrypted`` column is a Fernet-encrypted
                               JSON blob (shape differs per type; see below).
  * ``ProjectSlackWebhook``  — per-project Slack incoming webhooks. The full
                               URL is Fernet-encrypted at rest.

Used by test-flow alerts. All credential columns use ``app.utils.encryption``
(reuses ``TOKEN_ENCRYPTION_KEY``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

# ---------------------------------------------------------------------------
# Email sender types
# ---------------------------------------------------------------------------
EMAIL_SENDER_SMTP = "smtp"
EMAIL_SENDER_SES = "ses"
VALID_EMAIL_SENDER_TYPES = {EMAIL_SENDER_SMTP, EMAIL_SENDER_SES}

# Shape of ``ProjectEmailSender.config_encrypted`` (after decrypt_json):
#
#   type = 'smtp':
#     {
#       "host": "smtp.gmail.com",
#       "port": 587,
#       "username": "...",
#       "password": "...",
#       "tls_mode": "starttls"  # 'none' | 'starttls' | 'ssl'
#     }
#
#   type = 'ses':
#     {
#       "region": "us-east-1",
#       "access_key_id": "...",
#       "secret_access_key": "..."
#     }


# ---------------------------------------------------------------------------
# ProjectEmailSender
# ---------------------------------------------------------------------------
class ProjectEmailSender(Base):
    __tablename__ = "project_email_senders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)

    # Fernet-encrypted JSON blob — see shape comment above.
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Populated by the "Send test" button in project settings UI
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "type IN ('smtp', 'ses')",
            name="ck_email_sender_type_valid",
        ),
    )

    # ---- convenience: (de)serialize the encrypted config ----
    def get_config(self) -> dict[str, Any]:
        """Decrypt the stored config blob. Raises on tamper / wrong key."""
        from app.utils.encryption import decrypt_json

        return decrypt_json(self.config_encrypted)

    def set_config(self, config: dict[str, Any]) -> None:
        """Encrypt + store a new config blob."""
        from app.utils.encryption import encrypt_json

        self.config_encrypted = encrypt_json(config)


# ---------------------------------------------------------------------------
# ProjectSlackWebhook
# ---------------------------------------------------------------------------
class ProjectSlackWebhook(Base):
    __tablename__ = "project_slack_webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # e.g. "#marketing-daily" — free-text human label
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Full webhook URL, Fernet-encrypted at rest
    webhook_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # ---- convenience: (de)serialize the encrypted URL ----
    def get_webhook_url(self) -> str:
        from app.utils.encryption import decrypt_str

        return decrypt_str(self.webhook_url_encrypted)

    def set_webhook_url(self, url: str) -> None:
        from app.utils.encryption import encrypt_str

        self.webhook_url_encrypted = encrypt_str(url)
