import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class GA4Property(Base):
    """GA4 properties discovered after Google OAuth."""

    __tablename__ = "ga4_properties"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    property_id: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "properties/123456789"
    property_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_ga4_connection_active", connection_id, is_active),)


class GTMContainer(Base):
    """GTM containers discovered after Google OAuth."""

    __tablename__ = "gtm_containers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    container_id: Mapped[str] = mapped_column(String(50), nullable=False)
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_id: Mapped[str | None] = mapped_column(String(50), nullable=True)  # GTM-XXXXXXX
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_gtm_connection_account_active", connection_id, account_id, is_active),)


class GoogleAdsAccount(Base):
    """Google Ads accounts discovered after Google OAuth."""

    __tablename__ = "google_ads_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_gads_connection_customer_active", connection_id, customer_id, is_active),)


class MetaAdsAccount(Base):
    """Meta (Facebook) Ads accounts discovered after OAuth."""

    __tablename__ = "meta_ads_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # e.g. act_123456789
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_meta_connection_account_active", connection_id, account_id, is_active),)


class TikTokAdsAccount(Base):
    """TikTok Ads accounts discovered after OAuth."""

    __tablename__ = "tiktok_ads_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    advertiser_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_tiktok_connection_advertiser_active", connection_id, advertiser_id, is_active),
    )


class SearchConsoleSite(Base):
    """Google Search Console sites discovered after Google OAuth."""

    __tablename__ = "search_console_sites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Canonical GSC site identifier: either a URL-prefix property
    # ("https://example.com/") or a Domain property ("sc-domain:example.com").
    site_url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    permission_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_domain_property: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_gsc_connection_site_active", connection_id, site_url, is_active),)


class SnapAdsAccount(Base):
    """Snapchat Ads accounts discovered after OAuth."""

    __tablename__ = "snap_ads_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            "idx_snap_connection_org_account_active", connection_id, organization_id, account_id, is_active
        ),
    )
