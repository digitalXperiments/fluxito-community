import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

USER_TYPE_CLIENT = "client"
USER_TYPE_TEAM = "team"

AUTH_PROVIDER_GOOGLE = "google"
AUTH_PROVIDER_EMAIL = "email"
AUTH_PROVIDER_BOTH = "both"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # User type: 'client' (external users of the product) vs 'team' (internal staff)
    user_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default=USER_TYPE_CLIENT)

    # Interactive onboarding tutorial completion timestamp (NULL = not completed)
    tutorial_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # --- Email/password auth ---
    # NULL password_hash means the user signed up via Google (no password set).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Email verification
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # Auth provider: 'google', 'email', or 'both' (linked accounts)
    auth_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=AUTH_PROVIDER_GOOGLE
    )

    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Set when a super admin deactivates the account (as opposed to the user
    # pausing it themselves). Blocks every session and token, and the user
    # can't self-reactivate — only a super admin can lift it.
    deactivated_by_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # --- Onboarding preferences (set in the wizard, editable in Profile) ---
    preferred_ai_client: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("idx_email_active", email, is_active),)

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email})>"
