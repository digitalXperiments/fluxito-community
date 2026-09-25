"""
Shared helpers for MCP tool modules.

Centralizes common patterns used across marketing_tools, cross_platform_tools,
template_tools, analytics_tools, and warehouse_tools to eliminate duplication.

All data-access helpers resolve connections from ProjectContext first (project-
scoped), falling back to UserContext only when no active project is set.
"""

from typing import Any

import app.app_state as state
from app.config import settings
from app.models.credential_connection import BrazeConnection, MoengageConnection

# ---------------------------------------------------------------------------
# User / project / connection helpers
# ---------------------------------------------------------------------------


def get_current_user() -> Any | None:
    """Return the current MCP user context, or None."""
    return state.current_user_ctx.get()


def get_current_project() -> Any | None:
    """Return the current MCP project context, or None."""
    return state.current_project_ctx.get()


def get_google_conn_id() -> str | None:
    """
    Return the Google OAuth connection_id scoped to the active project.
    Falls back to user-level connections only if no project is active.
    """
    # Prefer project-scoped connections
    project = get_current_project()
    connections = None
    if project and project.connections:
        connections = project.connections
    else:
        user = get_current_user()
        if user and user.connections:
            connections = user.connections

    if not connections:
        return None

    for conn in connections:
        if getattr(conn, "provider", "google") in ("google", "", None):
            return conn.id
    return connections[0].id


def get_oauth_app_project_id() -> str | None:
    """Project whose OAuth app issued the tokens `get_provider_token` returns.

    Tokens come from the active project's connections, so the project's own
    OAuth app (if it has one) must be used to refresh / sign calls with them.
    None when tokens fall back to user-level connections → the install app.
    """
    project = get_current_project()
    if project and project.connections:
        return project.project_id
    return None


def get_provider_token(provider_str: str) -> str | None:
    """
    Return the decrypted access token for a non-Google OAuth provider
    (meta, tiktok, snap) stored in OAuthConnection rows.
    Scoped to the active project first; falls back to user context.
    Returns None if no connection exists for this provider.
    """
    # Prefer project-scoped connections
    project = get_current_project()
    connections = None
    if project and project.connections:
        connections = project.connections
    else:
        user = get_current_user()
        if user and user.connections:
            connections = user.connections

    if not connections:
        return None

    for conn in connections:
        if getattr(conn, "provider", "") == provider_str:
            try:
                return state.token_manager.decrypt(conn.access_token_encrypted)
            except Exception:
                return None
    return None


def get_provider_oauth1_tokens(provider_str: str) -> tuple[str, str] | None:
    """Return decrypted OAuth 1.0a access token and token secret for a provider."""
    project = get_current_project()
    connections = None
    if project and project.connections:
        connections = project.connections
    else:
        user = get_current_user()
        if user and user.connections:
            connections = user.connections

    if not connections:
        return None

    for conn in connections:
        if getattr(conn, "provider", "") == provider_str:
            try:
                return (
                    state.token_manager.decrypt(conn.access_token_encrypted),
                    state.token_manager.decrypt(conn.refresh_token_encrypted),
                )
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# "No connection" response factory
# ---------------------------------------------------------------------------


def no_connection_response(platform: str, connect_path: str, message: str) -> dict[str, Any]:
    """Build a standard 'connection_missing' error response."""
    base_url = settings.APP_BASE_URL
    connect_url = f"{base_url}{connect_path}"
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": message,
        "connect_url": connect_url,
        "action_required": f"Visit {connect_url} to connect.",
    }


# ---------------------------------------------------------------------------
# Credential fetcher (encrypted DB credentials) — project-scoped
# ---------------------------------------------------------------------------


def _resolve_project_id() -> str | None:
    """Return the active project_id string, or None."""
    project = get_current_project()
    return project.project_id if project else None


# Credential connection model → RBAC provider key(s) (app.auth.permissions.PROVIDERS).
_MODEL_PROVIDERS: dict[str, tuple[str, ...]] = {
    "BQConnection": ("bigquery",),
    "RedshiftConnection": ("redshift",),
    "SnowflakeConnection": ("snowflake",),
    "AmplitudeConnection": ("amplitude",),
    "MixpanelConnection": ("mixpanel",),
    "PostHogConnection": ("posthog",),
    "BrazeConnection": ("braze",),
    "MoengageConnection": ("moengage",),
    "AdobeConnection": ("adobe_analytics", "adobe_launch"),
    "MarketoConnection": ("adobe_marketo",),
}


async def _provider_allowed(model_class: type) -> bool:
    """Per-provider RBAC for credential connections (e.g. a role without Snowflake).

    Tools read these rows straight from the DB, so the provider filter applied
    to the project context's connection list never reached them.
    """
    providers = _MODEL_PROVIDERS.get(getattr(model_class, "__name__", ""))
    if not providers:
        return True
    user = get_current_user()
    project_id = _resolve_project_id()
    if not user or not project_id:
        return True
    from app.auth.permissions import resolve_effective_permissions

    eff = await resolve_effective_permissions(str(user.user_id), str(project_id))
    return any(eff.allows_provider(p) for p in providers)


async def get_encrypted_credential_conn(
    model_class: type,
    user_id: str,
    connection_id: str | None = None,
    extra_filters: list[Any] | None = None,
) -> Any | None:
    """
    Generic fetcher for credential-based connections (Amplitude, Adobe,
    Redshift, Snowflake, BigQuery).

    Scopes to the active project when available; falls back to user_id.
    Returns the ORM row or None.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.bq_connection import BQConnection

    if not await _provider_allowed(model_class):
        return None
    db_session = state.db_session_factory()
    project_id = _resolve_project_id()

    async with db_session as db:
        stmt = select(model_class).where(model_class.is_active == True)

        # Project-scoped when active project exists
        if project_id:
            pid = _uuid.UUID(project_id)
            # BQConnection uses fluxito_project_id instead of project_id
            if model_class is BQConnection:
                stmt = stmt.where(model_class.fluxito_project_id == pid)
            else:
                stmt = stmt.where(model_class.project_id == pid)
        else:
            # Fallback to user-scoped (no active project)
            stmt = stmt.where(model_class.user_id == _uuid.UUID(user_id))

        if connection_id:
            stmt = stmt.where(model_class.id == _uuid.UUID(connection_id))
        if extra_filters:
            for f in extra_filters:
                stmt = stmt.where(f)
        stmt = stmt.limit(1)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()


async def list_active_connections(model_class: type, user_id: str) -> list[Any]:
    """
    List all active connections of a given type.
    Scoped to the active project when available; falls back to user_id.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.bq_connection import BQConnection

    if not await _provider_allowed(model_class):
        return []
    db_session = state.db_session_factory()
    project_id = _resolve_project_id()

    async with db_session as db:
        stmt = select(model_class).where(model_class.is_active == True)

        if project_id:
            pid = _uuid.UUID(project_id)
            if model_class is BQConnection:
                stmt = stmt.where(model_class.fluxito_project_id == pid)
            else:
                stmt = stmt.where(model_class.project_id == pid)
        else:
            stmt = stmt.where(model_class.user_id == _uuid.UUID(user_id))

        result = await db.execute(stmt)
        return result.scalars().all()


def decrypt_field(encrypted_value: str) -> str:
    """Decrypt a Fernet-encrypted field using the configured encryption key."""
    from cryptography.fernet import Fernet

    f = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    return f.decrypt(encrypted_value.encode()).decode()


# ---------------------------------------------------------------------------
# Amplitude / Adobe credential helpers (public, for cross_platform + analytics)
# ---------------------------------------------------------------------------


async def get_amplitude_creds(user_id: str) -> tuple[str | None, str | None, str | None]:
    """
    Fetch user's active Amplitude connection and return (conn_id, api_key, secret_key).
    Returns (None, None, None) if no active connection.
    """
    from app.models.credential_connection import AmplitudeConnection

    conn = await get_encrypted_credential_conn(AmplitudeConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    secret_key = decrypt_field(conn.secret_key_encrypted)
    return str(conn.id), api_key, secret_key


async def get_adobe_analytics_creds(
    user_id: str, org_id: str | None = None
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """
    Fetch user's active Adobe Analytics connection and return
    (conn_id, client_id, client_secret, org_id, company_id).
    Returns (None, None, None, None, None) if no active connection.
    """
    from app.models.credential_connection import AdobeConnection

    extra = [AdobeConnection.has_analytics == True] if org_id is None else None
    conn = await get_encrypted_credential_conn(AdobeConnection, user_id, extra_filters=extra)
    if not conn:
        return None, None, None, None, None
    client_id = decrypt_field(conn.client_id_encrypted)
    client_secret = decrypt_field(conn.client_secret_encrypted)
    resolved_org = org_id or conn.org_id
    return str(conn.id), client_id, client_secret, resolved_org, conn.company_id


async def get_mixpanel_creds(
    user_id: str,
) -> tuple[str | None, str | None, str | None]:
    """
    Resolve active Mixpanel connection credentials for a user.
    Returns (conn_id, api_secret, service_token).
    Returns (None, None, None) if no active connection.
    """
    from app.models.credential_connection import MixpanelConnection

    conn = await get_encrypted_credential_conn(MixpanelConnection, user_id)
    if not conn:
        return None, None, None
    api_secret = decrypt_field(conn.api_key_encrypted)
    service_token = decrypt_field(conn.secret_key_encrypted)
    return str(conn.id), api_secret, service_token


async def get_posthog_creds(
    user_id: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Resolve active PostHog connection credentials for a user.
    Returns (conn_id, api_key, project_host, project_id).
    Returns (None, None, None, None) if no active connection.
    """
    from app.models.credential_connection import PostHogConnection

    conn = await get_encrypted_credential_conn(PostHogConnection, user_id)
    if not conn:
        return None, None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    return str(conn.id), api_key, conn.project_host, conn.external_project_id


async def get_braze_creds(user_id: str) -> dict[str, Any] | None:
    """
    Fetch user's active Braze connection and return connection details.
    Returns None if no active connection.
    """
    conn = await get_encrypted_credential_conn(BrazeConnection, user_id)
    if not conn:
        return None
    api_key = decrypt_field(conn.api_key_encrypted)
    return {
        "connection_id": str(conn.id),
        "display_name": conn.display_name,
        "rest_endpoint_url": conn.rest_endpoint_url,
        "api_key": api_key,
    }


async def get_moengage_creds(user_id: str) -> dict[str, Any] | None:
    """
    Fetch user's active MoEngage connection and return connection details.
    Returns None if no active connection.
    """
    conn = await get_encrypted_credential_conn(MoengageConnection, user_id)
    if not conn:
        return None
    api_key = decrypt_field(conn.api_key_encrypted)
    return {
        "connection_id": str(conn.id),
        "display_name": conn.display_name,
        "data_center": conn.data_center,
        "app_id": conn.app_id,
        "api_key": api_key,
    }
