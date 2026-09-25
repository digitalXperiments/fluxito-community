"""Resolve which connectors a project has connected, as a has_* flags object.

The Home route (``app.api.google_oauth_routes``) already builds an equivalent
``conn_flags`` namespace inline for its connector counter. This helper packages
the same query + flag-derivation logic so other pages — notably Project
Settings — can determine the connected set the SAME way, keying off the identical
``has_*`` attribute names that ``GRANULAR_CONNECTOR_CATALOG`` and
``app.connectors.rate_limits`` expect.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app import app_state


async def resolve_connection_flags(
    user_id: str | uuid.UUID,
    project_id: str | uuid.UUID | None,
) -> SimpleNamespace:
    """Return a has_* namespace describing the project's active connections.

    Mirrors the Home route's connection resolution: OAuth providers (with Google
    sub-services derived from granted scopes), plus the credential-based
    warehouse / analytics / marketing connectors. Resilient by design — any query
    failure yields all-False rather than raising, so a settings page still renders.
    """
    from app.models.bq_connection import BQConnection
    from app.models.connection import OAuthConnection
    from app.models.credential_connection import (
        AdjustConnection,
        AdobeConnection,
        AmplitudeConnection,
        AppsFlyerConnection,
        BranchConnection,
        BrazeConnection,
        MarketoConnection,
        MixpanelConnection,
        MoengageConnection,
        PostHogConnection,
        RedshiftConnection,
        SnowflakeConnection,
    )

    flags = SimpleNamespace(
        has_ga4=False,
        has_gtm=False,
        has_ads=False,
        has_gsc=False,
        has_bq=False,
        has_redshift=False,
        has_snowflake=False,
        has_amplitude=False,
        has_branch=False,
        has_appsflyer=False,
        has_adjust=False,
        has_mixpanel=False,
        has_posthog=False,
        has_braze=False,
        has_moengage=False,
        has_adobe_analytics=False,
        has_adobe_launch=False,
        has_marketo=False,
        has_meta=False,
        has_tiktok=False,
        has_snap=False,
        has_x=False,
        has_reddit=False,
        has_apple=False,
        has_linkedin=False,
        has_pinterest=False,
        has_bing=False,
    )

    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    pid: uuid.UUID | None = None
    if project_id is not None:
        pid = project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id))

    try:
        async with app_state.db_session_factory() as db:
            # OAuth connections (Google + the ad platforms).
            oauth_stmt = select(OAuthConnection).where(
                OAuthConnection.user_id == uid,
                OAuthConnection.is_active == True,
            )
            if pid is not None:
                oauth_stmt = oauth_stmt.where(OAuthConnection.project_id == pid)
            oauth_conns = list((await db.execute(oauth_stmt)).scalars().all())

            provider_flag = {
                "meta": "has_meta",
                "tiktok": "has_tiktok",
                "snap": "has_snap",
                "linkedin": "has_linkedin",
                "pinterest": "has_pinterest",
                "x": "has_x",
                "reddit": "has_reddit",
                "bing": "has_bing",
                "apple": "has_apple",
            }
            for c in oauth_conns:
                attr = provider_flag.get(c.provider or "")
                if attr:
                    setattr(flags, attr, True)
                # Google sub-services are derived from granted OAuth scopes.
                if (c.provider or "google") in ("google", None, ""):
                    scopes = c.scopes or []
                    if any("analytics" in s for s in scopes):
                        flags.has_ga4 = True
                    if any("tagmanager" in s for s in scopes):
                        flags.has_gtm = True
                    if "https://www.googleapis.com/auth/adwords" in scopes:
                        flags.has_ads = True
                    if any("webmasters" in s for s in scopes):
                        flags.has_gsc = True

            # Credential-based connectors. BigQuery scopes by fluxito_project_id.
            bq_stmt = select(BQConnection).where(
                BQConnection.user_id == uid,
                BQConnection.is_active == True,
            )
            if pid is not None:
                bq_stmt = bq_stmt.where(BQConnection.fluxito_project_id == pid)
            flags.has_bq = bool((await db.execute(bq_stmt)).scalars().first())

            credential_models = (
                (AmplitudeConnection, ("has_amplitude",)),
                (BranchConnection, ("has_branch",)),
                (AppsFlyerConnection, ("has_appsflyer",)),
                (AdjustConnection, ("has_adjust",)),
                (MixpanelConnection, ("has_mixpanel",)),
                (PostHogConnection, ("has_posthog",)),
                (BrazeConnection, ("has_braze",)),
                (MoengageConnection, ("has_moengage",)),
                (AdobeConnection, ("has_adobe_analytics", "has_adobe_launch")),
                (MarketoConnection, ("has_marketo",)),
                (RedshiftConnection, ("has_redshift",)),
                (SnowflakeConnection, ("has_snowflake",)),
            )
            for model, attrs in credential_models:
                stmt = select(model).where(
                    model.user_id == uid,
                    model.is_active == True,
                )
                if pid is not None:
                    stmt = stmt.where(model.project_id == pid)
                present = bool((await db.execute(stmt)).scalars().first())
                if present:
                    for attr in attrs:
                        setattr(flags, attr, True)
    except Exception:
        # A connection-resolution failure must never break the page that uses it.
        return flags

    return flags
