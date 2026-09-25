"""OAuth app credential lookup + CRUD service.

`get_oauth_app_credentials(db, platform, project_id=...)` is the canonical
read API used by every connector and route that needs an OAuth client
ID/secret. Two layers:

* the install-wide app (`oauth_app_credentials`, one row per platform),
  configured at /admin/oauth-apps (super admin access required);
* an optional per-project override (`project_oauth_app_credentials`),
  configured by project owners/admins in Project settings → OAuth apps.

Pass the project a connection belongs to and the project's own app wins
when it has one; otherwise the install's app is used. Sign-in to Fluxito
itself always uses the install app (call without ``project_id``).

A connection's tokens are bound to the app that issued them, so changing or
removing a project's app for a platform marks that project's existing
connections for it as needing a reconnect (`mark_connections_for_reconnect`).

Decryption uses ``TOKEN_ENCRYPTION_KEY`` (same Fernet key as user
OAuth tokens). Plaintext is only held in memory; never logged.

A 5-minute in-memory cache fronts the DB read so token-refresh
hot paths don't hit Postgres on every call. The cache is invalidated
on `upsert_oauth_app_credentials` / `delete_oauth_app_credentials`
within the same Python process; multi-worker installs converge
within ``_CACHE_TTL_SEC`` seconds.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_app_credential import SUPPORTED_PLATFORMS, OAuthAppCredential, ProjectOAuthAppCredential
from app.utils.encryption import decrypt_str, encrypt_str


class OAuthAppNotConfigured(Exception):
    """Raised when a platform has no DB row configured."""


@dataclass(frozen=True)
class OAuthAppCreds:
    """Resolved OAuth app credentials for a platform.

    `source` is 'db' for the install-wide app and 'project' for a project's
    own app override.
    """

    platform: str
    client_id: str
    client_secret: str
    extra: dict[str, Any]
    source: str


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------

_CacheKey = tuple[str, str | None]  # (platform, project_id or None for the install app)
_CACHE: dict[_CacheKey, tuple[float, OAuthAppCreds]] = {}
_CACHE_TTL_SEC = 300


def _cache_get(platform: str, project_id: Any = None) -> OAuthAppCreds | None:
    key = (platform, str(project_id) if project_id else None)
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, creds = entry
    if time.monotonic() - ts >= _CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return creds


def _cache_put(platform: str, creds: OAuthAppCreds, project_id: Any = None) -> None:
    _CACHE[(platform, str(project_id) if project_id else None)] = (time.monotonic(), creds)


def _cache_invalidate(platform: str, project_id: Any = None) -> None:
    """Drop cached entries for a platform.

    The install app is the fallback for every project, so invalidating it
    (``project_id=None``) drops every project's entry for the platform too.
    """
    if project_id:
        _CACHE.pop((platform, str(project_id)), None)
        return
    for key in [k for k in _CACHE if k[0] == platform]:
        _CACHE.pop(key, None)


def _cache_clear_all() -> None:
    """Clear the entire cache. Used by tests."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------


def _decrypt_secret(raw: bytes | str) -> str:
    return decrypt_str(raw.decode() if isinstance(raw, bytes) else raw)


def _as_uuid(project_id: Any) -> uuid.UUID | None:
    if not project_id:
        return None
    if isinstance(project_id, uuid.UUID):
        return project_id
    try:
        return uuid.UUID(str(project_id))
    except (ValueError, TypeError):
        return None


async def get_project_oauth_app_credentials(
    db: AsyncSession, platform: str, project_id: Any
) -> OAuthAppCreds | None:
    """Return the project's own app for `platform`, or None when it has none."""
    pid = _as_uuid(project_id)
    if pid is None:
        return None
    row = (
        await db.execute(
            select(ProjectOAuthAppCredential).where(
                ProjectOAuthAppCredential.project_id == pid,
                ProjectOAuthAppCredential.platform == platform,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return OAuthAppCreds(
        platform=platform,
        client_id=row.client_id,
        client_secret=_decrypt_secret(row.client_secret),
        extra=dict(row.extra_json) if row.extra_json else {},
        source="project",
    )


async def get_oauth_app_credentials(db: AsyncSession, platform: str, project_id: Any = None) -> OAuthAppCreds:
    """Return OAuth credentials for `platform`.

    Resolution order:
      1. `project_id` given and that project has its own app → it (source='project').
      2. Install-wide row in `oauth_app_credentials` (source='db').
      3. Raise `OAuthAppNotConfigured`.

    This call is **uncached** — every invocation hits the DB. For hot paths
    (e.g. token refresh on every tool call), use `get_oauth_app_credentials_cached`.
    """
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")

    own = await get_project_oauth_app_credentials(db, platform, project_id)
    if own is not None:
        return own

    row = (
        await db.execute(select(OAuthAppCredential).where(OAuthAppCredential.platform == platform))
    ).scalar_one_or_none()

    if row is not None:
        return OAuthAppCreds(
            platform=platform,
            client_id=row.client_id,
            client_secret=_decrypt_secret(row.client_secret),
            extra=dict(row.extra_json) if row.extra_json else {},
            source="db",
        )

    raise OAuthAppNotConfigured(
        f"OAuth app for {platform!r} is not configured. Add the project's own app in "
        f"Project settings → OAuth apps, or ask a super admin to configure it at /admin/oauth-apps."
    )


async def get_oauth_app_credentials_cached(
    db: AsyncSession, platform: str, project_id: Any = None
) -> OAuthAppCreds:
    """Cached variant of `get_oauth_app_credentials` (5-minute TTL).

    Use on hot paths like token refresh. Cache invalidated on upsert/delete
    within the same process; multi-worker installs converge within
    `_CACHE_TTL_SEC` seconds of an update.
    """
    cached = _cache_get(platform, project_id)
    if cached is not None:
        return cached
    creds = await get_oauth_app_credentials(db, platform, project_id)
    _cache_put(platform, creds, project_id)
    return creds


async def list_oauth_app_status(db: AsyncSession) -> list[dict[str, Any]]:
    """Return one entry per supported platform.

    Each entry has: platform, source ('db' | 'unconfigured'),
    client_id_masked (None when unconfigured), updated_at (only for DB rows).
    Used by the settings UI to render the platform grid.
    """
    rows = (await db.execute(select(OAuthAppCredential))).scalars().all()
    db_map = {r.platform: r for r in rows}

    out: list[dict[str, Any]] = []
    for platform in SUPPORTED_PLATFORMS:
        if platform in db_map:
            r = db_map[platform]
            out.append(
                {
                    "platform": platform,
                    "source": "db",
                    "client_id_masked": _mask(r.client_id),
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )
        else:
            out.append(
                {
                    "platform": platform,
                    "source": "unconfigured",
                    "client_id_masked": None,
                    "updated_at": None,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Write APIs
# ---------------------------------------------------------------------------


async def upsert_oauth_app_credentials(
    db: AsyncSession,
    *,
    platform: str,
    client_id: str,
    client_secret: str,
    extra: dict[str, Any] | None,
    configured_by_user_id,
) -> OAuthAppCredential:
    """Insert or update the row for a platform. Caller commits the session."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")

    row = (
        await db.execute(select(OAuthAppCredential).where(OAuthAppCredential.platform == platform))
    ).scalar_one_or_none()

    if row is None:
        row = OAuthAppCredential(
            platform=platform,
            client_id=client_id,
            client_secret=encrypt_str(client_secret).encode(),
            extra_json=(extra or None),
            configured_by_user_id=configured_by_user_id,
        )
        db.add(row)
    else:
        row.client_id = client_id
        row.client_secret = encrypt_str(client_secret).encode()
        row.extra_json = extra or None
        row.configured_by_user_id = configured_by_user_id

    _cache_invalidate(platform)
    return row


async def delete_oauth_app_credentials(db: AsyncSession, *, platform: str) -> bool:
    """Delete the row for a platform. Caller commits. Returns True if deleted."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")

    row = (
        await db.execute(select(OAuthAppCredential).where(OAuthAppCredential.platform == platform))
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    _cache_invalidate(platform)
    return True


# ---------------------------------------------------------------------------
# Per-project overrides
# ---------------------------------------------------------------------------


async def list_project_oauth_app_status(db: AsyncSession, project_id: Any) -> list[dict[str, Any]]:
    """One entry per platform for a project's OAuth apps page.

    ``source`` is 'project' (the project's own app), 'db' (falls back to the
    install app) or 'unconfigured' (neither — the platform can't be connected).
    """
    pid = _as_uuid(project_id)
    own = {}
    if pid is not None:
        rows = (
            (
                await db.execute(
                    select(ProjectOAuthAppCredential).where(ProjectOAuthAppCredential.project_id == pid)
                )
            )
            .scalars()
            .all()
        )
        own = {r.platform: r for r in rows}
    install = {r.platform for r in (await db.execute(select(OAuthAppCredential))).scalars().all()}

    out: list[dict[str, Any]] = []
    for platform in SUPPORTED_PLATFORMS:
        r = own.get(platform)
        if r is not None:
            out.append(
                {
                    "platform": platform,
                    "source": "project",
                    "client_id_masked": _mask(r.client_id),
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    "install_configured": platform in install,
                }
            )
        else:
            out.append(
                {
                    "platform": platform,
                    "source": "db" if platform in install else "unconfigured",
                    "client_id_masked": None,
                    "updated_at": None,
                    "install_configured": platform in install,
                }
            )
    return out


async def upsert_project_oauth_app_credentials(
    db: AsyncSession,
    *,
    project_id: Any,
    platform: str,
    client_id: str,
    client_secret: str,
    extra: dict[str, Any] | None,
    configured_by_user_id,
) -> bool:
    """Insert or update a project's own app. Caller commits.

    Returns True when the app the project connects through changed (a new
    override, or a different client ID) — existing connections for the
    platform then hold tokens from the old app and must be reconnected.
    Rotating only the secret of the same client ID keeps them working.
    """
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")
    pid = _as_uuid(project_id)
    if pid is None:
        raise ValueError("project_id is required")

    row = (
        await db.execute(
            select(ProjectOAuthAppCredential).where(
                ProjectOAuthAppCredential.project_id == pid,
                ProjectOAuthAppCredential.platform == platform,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        db.add(
            ProjectOAuthAppCredential(
                project_id=pid,
                platform=platform,
                client_id=client_id,
                client_secret=encrypt_str(client_secret).encode(),
                extra_json=(extra or None),
                configured_by_user_id=configured_by_user_id,
            )
        )
        app_changed = True
    else:
        app_changed = row.client_id != client_id
        row.client_id = client_id
        row.client_secret = encrypt_str(client_secret).encode()
        row.extra_json = extra or None
        row.configured_by_user_id = configured_by_user_id

    _cache_invalidate(platform, pid)
    return app_changed


async def delete_project_oauth_app_credentials(db: AsyncSession, *, project_id: Any, platform: str) -> bool:
    """Remove a project's own app (it falls back to the install app). Caller commits."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    pid = _as_uuid(project_id)
    if pid is None:
        return False
    row = (
        await db.execute(
            select(ProjectOAuthAppCredential).where(
                ProjectOAuthAppCredential.project_id == pid,
                ProjectOAuthAppCredential.platform == platform,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    _cache_invalidate(platform, pid)
    return True


async def mark_connections_for_reconnect(db: AsyncSession, *, project_id: Any, platform: str) -> int:
    """Flag a project's active connections for `platform` as needing a reconnect.

    Called when the project's app for the platform changes: their tokens
    were issued to the previous app and won't refresh through the new one.
    Sets ``connection_status='broken'`` — the state the UI already shows as
    "Reconnect". Caller commits. Returns the number of connections flagged.
    """
    from sqlalchemy import update

    from app.models.connection import OAuthConnection

    pid = _as_uuid(project_id)
    if pid is None:
        return 0
    result = await db.execute(
        update(OAuthConnection)
        .where(
            OAuthConnection.project_id == pid,
            OAuthConnection.provider == platform,
            OAuthConnection.is_active.is_(True),
        )
        .values(connection_status="broken")
    )
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask(client_id: str) -> str:
    """Mask a client_id for display: keep first 6 + last 4."""
    if not client_id:
        return ""
    if len(client_id) <= 12:
        return "***"
    return f"{client_id[:6]}…{client_id[-4:]}"
