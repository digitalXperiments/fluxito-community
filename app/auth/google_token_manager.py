"""
Google Token Manager

Handles all Google OAuth token operations:
  - Returning valid access tokens (from cache or decrypted from DB)
  - Silent refresh when token is within 5 minutes of expiry
  - Fernet encryption/decryption
  - Marking connections as broken on 401 refresh failures

Performance optimizations:
  - Shared httpx.AsyncClient with connection pooling (reused across all refreshes)
  - Redis-first token retrieval
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from app.config import settings
from app.models.connection import OAuthConnection

logger = logging.getLogger(__name__)

# Refresh buffer: refresh token if it expires in this many minutes
_TOKEN_REFRESH_BUFFER_MINUTES = 5


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo).

    The ``token_expiry`` column is TIMESTAMP WITHOUT TIME ZONE, so all
    comparisons and writes must use naive datetimes.  ``datetime.now(UTC)``
    produces a tz-aware value that asyncpg rejects when binding to that column.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class GoogleConnectionBrokenError(Exception):
    def __init__(self, connection_id: str):
        self.connection_id = connection_id
        super().__init__(f"Google connection {connection_id} is broken — token refresh failed")


class GoogleTokenManager:
    def __init__(self, redis, db_session_factory):
        self.redis = redis
        self.db_session_factory = db_session_factory
        self.fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
        # Shared HTTP client with connection pooling — reused for all token refreshes
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
        )

    async def close(self):
        """Shutdown the shared HTTP client. Call during app shutdown."""
        await self._http_client.aclose()

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode()

    async def get_valid_access_token(self, connection_id: str) -> str:
        """
        Returns a valid Google access token.
        Checks Redis cache first; decrypts from DB; auto-refreshes if near expiry.
        """
        # Check Redis cache first
        cached = await self.redis.get(f"gtoken:{connection_id}")
        if cached:
            return cached.decode()

        # Load from DB
        async with self.db_session_factory() as db:
            result = await db.execute(select(OAuthConnection).where(OAuthConnection.id == connection_id))
            connection = result.scalar_one_or_none()

        if not connection:
            raise ValueError(f"Connection {connection_id} not found")

        # Check if refresh needed (5 min buffer)
        # Use naive UTC to match TIMESTAMP WITHOUT TIME ZONE column
        now = _utcnow()
        needs_refresh = connection.token_expiry is None or connection.token_expiry < now + timedelta(
            minutes=_TOKEN_REFRESH_BUFFER_MINUTES
        )

        if needs_refresh:
            return await self._refresh_google_token(connection)

        # Decrypt and cache
        access_token = self.decrypt(connection.access_token_encrypted)
        ttl = int((connection.token_expiry - now).total_seconds())
        cache_ttl = max(ttl - 60, 30)
        await self.redis.setex(f"gtoken:{connection_id}", cache_ttl, access_token)
        return access_token

    async def _refresh_google_token(self, connection: OAuthConnection) -> str:
        """Refresh Google access token using stored refresh token."""
        refresh_token = self.decrypt(connection.refresh_token_encrypted)

        # Resolve app credentials (cached — avoid DB hit on every token refresh)
        from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached

        async with self.db_session_factory() as _cred_db:
            _creds = await get_oauth_app_credentials_cached(
                _cred_db, "google", project_id=connection.project_id
            )

        # Use shared pooled client instead of creating a new one each time
        resp = await self._http_client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _creds.client_id,
                "client_secret": _creds.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if resp.status_code != 200:
            logger.warning(
                "Token refresh failed for connection %s: %d",
                connection.id,
                resp.status_code,
            )
            await self._mark_connection_broken(str(connection.id))
            raise GoogleConnectionBrokenError(str(connection.id))

        try:
            data = resp.json()
        except (ValueError, KeyError) as e:
            logger.warning(
                "Failed to parse token response for connection %s: %s",
                connection.id,
                str(e),
            )
            await self._mark_connection_broken(str(connection.id))
            raise GoogleConnectionBrokenError(str(connection.id))

        new_token = data.get("access_token")
        expires_in = data.get("expires_in")

        if not new_token or not expires_in:
            logger.warning("Token response missing required fields for connection %s", connection.id)
            await self._mark_connection_broken(str(connection.id))
            raise GoogleConnectionBrokenError(str(connection.id))

        new_expiry = _utcnow() + timedelta(seconds=expires_in)

        # Update DB with re-encrypted token
        async with self.db_session_factory() as db:
            await db.execute(
                update(OAuthConnection)
                .where(OAuthConnection.id == connection.id)
                .values(
                    access_token_encrypted=self.encrypt(new_token),
                    token_expiry=new_expiry,
                )
            )
            await db.commit()

        # Cache new token
        cache_ttl = max(expires_in - 60, 30)
        await self.redis.setex(f"gtoken:{connection.id!s}", cache_ttl, new_token)
        return new_token

    async def _mark_connection_broken(self, connection_id: str):
        async with self.db_session_factory() as db:
            await db.execute(
                update(OAuthConnection)
                .where(OAuthConnection.id == connection_id)
                .values(connection_status="broken")
            )
            await db.commit()
        await self.redis.delete(f"gtoken:{connection_id}")
        # Also invalidate user context cache so the broken status is reflected
        # (We don't import directly to avoid circular imports; use Redis key pattern)

    async def store_new_tokens(
        self,
        connection_id: str,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ):
        """Store new tokens (called on data OAuth callback)."""
        expiry = _utcnow() + timedelta(seconds=expires_in)
        async with self.db_session_factory() as db:
            await db.execute(
                update(OAuthConnection)
                .where(OAuthConnection.id == connection_id)
                .values(
                    access_token_encrypted=self.encrypt(access_token),
                    refresh_token_encrypted=self.encrypt(refresh_token),
                    token_expiry=expiry,
                    connection_status="active",
                )
            )
            await db.commit()
        cache_ttl = max(expires_in - 60, 30)
        await self.redis.setex(f"gtoken:{connection_id}", cache_ttl, access_token)
