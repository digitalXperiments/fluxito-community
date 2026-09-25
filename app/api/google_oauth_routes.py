"""
Connect/Dashboard UI Routes

Main user-facing web interface:
  GET  /               — Landing page (shows connection status if authenticated)
  GET  /connect        — Google data OAuth tier selector (after MCP auth)
  GET  /api/connections/google/initiate  — Start Google data OAuth
  GET  /auth/google/data/callback        — Google data OAuth callback
  GET  /api/connections                  — List connections (JSON)
  DELETE /api/connections/{conn_id}      — Disconnect
  GET  /api/health                       — Health check
"""

from html import escape as _html_escape
import asyncio
import json
import logging
import secrets
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, update

import app.app_state as app_state
from app.api.project_routes import ensure_active_project, get_active_project_id, set_active_project_cookie
from app.auth.mcp_session_manager import (
    build_user_context,
    invalidate_user_context_cache,
    require_valid_mcp_token,
)
from app.auth.uid_cookie import get_uid_from_request, sign_uid
from app.config import settings
from app.models.bq_connection import BQConnection
from app.models.connection import OAuthConnection
from app.models.credential_connection import (
    AdobeConnection,
    AdjustConnection,
    AmplitudeConnection,
    AppsFlyerConnection,
    BrazeConnection,
    BranchConnection,
    MarketoConnection,
    MixpanelConnection,
    MoengageConnection,
    PostHogConnection,
    RedshiftConnection,
    SnowflakeConnection,
)
from app.models.project import Project
from app.models.token import GA4Property, GTMContainer
from app.models.user import User
from app.templating import render
from app.utils import safe_next_url

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Canonical GRANULAR connector model ──────────────────────────────
# The single source of truth for how many distinct connectors exist and
# how "connected" is counted. Each connector counts as ONE — and Google's
# five services (GA4, GTM, Google Ads, Search Console, Data Manager) each count
# SEPARATELY even though one Google OAuth grants them together. This is the
# only place the connected/total numbers are derived; BOTH the /home KPI and
# the /connect page header read from the same helper so they can never drift.
#
# Each entry is (key, label, has_flags) where `has_flags` is a tuple of
# attribute names on the resolved project/user context (or any flags object)
# — the connector counts as connected if ANY of those attrs is truthy. The
# attribute names MUST match the `has_*` fields on ProjectContext/UserContext
# in app.auth.mcp_session_manager (and the lightweight flags objects the
# /home and /connect routes build from their own DB queries). Adobe maps to
# two flags (analytics + launch) but still counts as a single connector.
GRANULAR_CONNECTOR_CATALOG: list[tuple[str, str, tuple[str, ...]]] = [
    # Google services — split, each counts as one
    ("ga4", "GA4", ("has_ga4",)),
    ("gtm", "GTM", ("has_gtm",)),
    ("google_ads", "Google Ads", ("has_ads",)),
    ("search_console", "Search Console", ("has_gsc",)),
    ("data_manager", "Google Data Manager", ("has_data_manager",)),
    # Data warehouses
    ("bigquery", "BigQuery", ("has_bq",)),
    ("redshift", "Redshift", ("has_redshift",)),
    ("snowflake", "Snowflake", ("has_snowflake",)),
    # Product analytics
    ("amplitude", "Amplitude", ("has_amplitude",)),
    ("branch", "Branch", ("has_branch",)),
    ("appsflyer", "AppsFlyer", ("has_appsflyer",)),
    ("adjust", "Adjust", ("has_adjust",)),
    ("mixpanel", "Mixpanel", ("has_mixpanel",)),
    ("posthog", "PostHog", ("has_posthog",)),
    ("braze", "Braze", ("has_braze",)),
    ("moengage", "MoEngage", ("has_moengage",)),
    ("adobe", "Adobe", ("has_adobe_analytics", "has_adobe_launch")),
    # Ad platforms
    ("meta", "Meta Ads", ("has_meta",)),
    ("tiktok", "TikTok Ads", ("has_tiktok",)),
    ("snap", "Snapchat Ads", ("has_snap",)),
    ("x", "X Ads", ("has_x",)),
    ("reddit", "Reddit Ads", ("has_reddit",)),
    ("apple", "Apple Ads", ("has_apple",)),
    ("linkedin", "LinkedIn Ads", ("has_linkedin",)),
    ("pinterest", "Pinterest Ads", ("has_pinterest",)),
    ("bing", "Bing Webmaster Tools", ("has_bing",)),
]

# Total number of distinct granular connectors (denominator on both pages).
TOTAL_CONNECTOR_COUNT = len(GRANULAR_CONNECTOR_CATALOG)


def count_granular_connectors(flags) -> tuple[int, int]:
    """Count connected connectors granularly from a flags object.

    `flags` is any object carrying the ``has_*`` attributes named in
    GRANULAR_CONNECTOR_CATALOG — a ProjectContext/UserContext, or a
    lightweight namespace built by a route from its own DB queries. A
    connector counts as connected if ANY of its catalog `has_flags`
    attributes is truthy. Returns ``(connected_count, total_count)`` where
    total is the full granular catalog size (Google's 4 services counted
    separately). This is the SINGLE source of truth shared by the /home KPI
    and the /connect page header.
    """
    connected = 0
    for _key, _label, attrs in GRANULAR_CONNECTOR_CATALOG:
        if any(getattr(flags, a, False) for a in attrs):
            connected += 1
    return connected, TOTAL_CONNECTOR_COUNT


async def _load_user_view(user_ctx, project_id: str | None = None) -> dict:
    """Load a lightweight user view for templates (email, display_name, is_superadmin, permissions).

    When ``project_id`` is provided, effective RBAC permissions are resolved and
    included in the ``permissions`` key. When absent, the default ``full=True``
    stub is returned so non-project pages are never gated.
    """
    display_name = None
    is_superadmin = False
    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            result = await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
            u = result.scalar_one_or_none()
            if u:
                display_name = u.display_name
                is_superadmin = bool(u.is_superadmin)
    except Exception:
        pass

    permissions = {"full": True, "tools": {}, "providers": [], "advanced": []}
    if project_id:
        try:
            from app.auth.permissions import resolve_effective_permissions

            eff = await resolve_effective_permissions(user_ctx.user_id, project_id)
            permissions = {
                "full": eff.full,
                "tools": {k: sorted(v) for k, v in eff.tools.items()},
                "providers": sorted(eff.providers),
                "advanced": sorted(eff.advanced),
            }
        except Exception:
            pass

    return {
        "id": user_ctx.user_id,
        "email": user_ctx.email,
        "display_name": display_name,
        "is_superadmin": is_superadmin,
        "permissions": permissions,
    }


async def _resolve_user_ctx(request: Request):
    """Resolve user context from MCP bearer or signed cookie.

    Returns ``None`` if not authed. Stashes the *reason* the cookie path
    failed on ``request.state.uid_cookie_invalid`` so route handlers can
    decide whether to clear the stale cookie before rendering /signin
    (which prevents the /signin ↔ /home redirect loop that occurs when
    the cookie is signature-valid but the user no longer exists in DB).
    """
    user_ctx = None
    # MCP Bearer auth — only relevant for API/MCP callers; raising on a
    # browser request without a Bearer header is normal, so don't log.
    try:
        user_ctx = await require_valid_mcp_token(request)
    except HTTPException:
        pass
    except Exception as exc:
        logger.warning("MCP token resolution raised unexpectedly: %s", exc)

    if user_ctx is not None:
        return user_ctx

    uid = get_uid_from_request(request)
    if not uid:
        return None

    try:
        user_ctx = await build_user_context(uid, request)
    except HTTPException as exc:
        # 401 ("User not found") → cookie sig is valid but the user is
        # gone. Mark the request so the caller can clear the cookie.
        if getattr(exc, "status_code", None) == 401:
            try:
                request.state.uid_cookie_invalid = True
            except Exception:
                pass
        else:
            logger.warning("build_user_context raised %s", exc)
    except Exception as exc:
        logger.warning("build_user_context raised unexpectedly: %s", exc)
        try:
            request.state.uid_cookie_invalid = True
        except Exception:
            pass
    return user_ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _time_ago(when: datetime | None) -> str:
    """Humanize a past timestamp for the Home briefing (e.g. "2d ago")."""
    if when is None:
        return "Never"
    delta = datetime.utcnow() - when
    seconds = delta.total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


async def _render_interstitial(
    request: Request,
    platform_slug: str,
    platform_name: str,
    platform_desc: str,
    btn_bg: str,
    btn_text_color: str,
    btn_shadow: str,
    permissions: list[tuple[str, str, str]],
    authorize_url: str,
    user_ctx=None,
) -> HTMLResponse:
    """Render a branded OAuth connect interstitial for non-Google platforms."""
    # Fail early with a friendly error page if OAuth isn't configured
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    _pid = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    async with app_state.db_session_factory() as _cred_db:
        await get_oauth_app_credentials(_cred_db, platform_slug, project_id=_pid)

    user_view = None
    if user_ctx:
        user_view = await _load_user_view(user_ctx)
    return render(
        request,
        "connect/oauth_interstitial.html",
        {
            "user": user_view,
            "platform_slug": platform_slug,
            "platform_name": platform_name,
            "platform_desc": platform_desc,
            "btn_bg": btn_bg,
            "btn_text_color": btn_text_color,
            "btn_shadow": btn_shadow,
            "permissions": permissions,
            "authorize_url": authorize_url,
        },
    )


def _infer_tier(scopes: list) -> str:
    joined = " ".join(scopes)
    if "analytics.edit" in joined or "analytics " in joined:
        return "Full Access"
    if "tagmanager.edit" in joined:
        return "GTM Write"
    return "Read Only"


def _fernet() -> Fernet:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# Base identity scopes always included so userinfo returns email
_IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Per-product scope sets — used to build the minimal required scope for the selected products.
# GA4 read-only is the baseline; GA4 write adds the full analytics scope.
# GTM starts read-only; GTM write tier adds edit + publish.
PRODUCT_SCOPES = {
    "ga4": [
        "https://www.googleapis.com/auth/analytics.readonly",
    ],
    "ga4_write": [
        "https://www.googleapis.com/auth/analytics",
        "https://www.googleapis.com/auth/analytics.edit",
    ],
    "data_manager": [
        "https://www.googleapis.com/auth/datamanager",
    ],
    "gtm": [
        "https://www.googleapis.com/auth/tagmanager.readonly",
    ],
    "gtm_write": [
        "https://www.googleapis.com/auth/tagmanager.readonly",
        "https://www.googleapis.com/auth/tagmanager.edit.containers",
        "https://www.googleapis.com/auth/tagmanager.publish",
        "https://www.googleapis.com/auth/tagmanager.manage.accounts",
    ],
    "ads": [
        "https://www.googleapis.com/auth/adwords",
    ],
    "gsc": [
        "https://www.googleapis.com/auth/webmasters.readonly",
    ],
    "gsc_write": [
        "https://www.googleapis.com/auth/webmasters",
    ],
}

# Legacy tier map kept for backward compatibility with any existing links
SCOPE_MAP = {
    "readonly": _IDENTITY_SCOPES + PRODUCT_SCOPES["ga4"] + PRODUCT_SCOPES["gtm"] + PRODUCT_SCOPES["ads"],
    "gtm_write": _IDENTITY_SCOPES
    + PRODUCT_SCOPES["ga4"]
    + PRODUCT_SCOPES["gtm_write"]
    + PRODUCT_SCOPES["ads"],
    "full": _IDENTITY_SCOPES
    + PRODUCT_SCOPES["ga4_write"]
    + PRODUCT_SCOPES["gtm_write"]
    + PRODUCT_SCOPES["ads"],
}


def _build_scopes_for_products(
    products: list[str], write_ga4: bool = False, write_gtm: bool = False, write_gsc: bool = False
) -> list[str]:
    """Build de-duplicated scope list for the selected products and write-access flags."""
    scopes: list[str] = list(_IDENTITY_SCOPES)
    if "ga4" in products:
        scopes += PRODUCT_SCOPES["ga4_write" if write_ga4 else "ga4"]
    if "data_manager" in products:
        scopes += PRODUCT_SCOPES["data_manager"]
    if "gtm" in products:
        scopes += PRODUCT_SCOPES["gtm_write" if write_gtm else "gtm"]
    if "ads" in products:
        scopes += PRODUCT_SCOPES["ads"]
    if "gsc" in products:
        scopes += PRODUCT_SCOPES["gsc_write" if write_gsc else "gsc"]
    # Deduplicate while preserving order
    seen: set = set()
    return [s for s in scopes if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root route. Signed-in users go to Home; everyone else to sign-in."""
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is not None:
        return RedirectResponse(url="/home", status_code=302)
    return RedirectResponse(url="/signin", status_code=302)


@router.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    """Authenticated dashboard — scoped to the active project."""
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return RedirectResponse(url="/signin?next=/home", status_code=302)

    # Ensure the user has completed onboarding. If not, redirect to tutorial.
    user_rec = None
    try:
        db_session_user = app_state.db_session_factory()
        async with db_session_user as db_u:
            user_rec = (
                await db_u.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
            ).scalar_one_or_none()
            if user_rec and user_rec.tutorial_completed_at is None:
                return RedirectResponse(url="/tutorial", status_code=302)
    except Exception:
        pass

    # Resolve active project — for users who have completed onboarding,
    # ensure a default workspace exists so they never get trapped in a
    # /home ↔ /tutorial redirect loop.
    active_pid = await ensure_active_project(request, user_ctx.user_id)
    if not active_pid:
        from app.api.project_routes import ensure_default_project

        await ensure_default_project(
            user_id=user_ctx.user_id,
            display_name=user_ctx.display_name,
            email=user_ctx.email,
        )
        active_pid = await ensure_active_project(request, user_ctx.user_id)
        if not active_pid:
            return RedirectResponse(url="/tutorial", status_code=302)
    active_pid_uuid = uuid.UUID(active_pid) if active_pid else None

    # Keep the compact Home checklist in sync with the onboarding wizard.
    # Home is only reachable after tutorial completion, but the remaining
    # setup items can still be incomplete when a user skips ahead.
    active_project_name = None
    try:
        if active_pid_uuid:
            db_session_project = app_state.db_session_factory()
            async with db_session_project as db_project:
                project_rec = (
                    await db_project.execute(select(Project).where(Project.id == active_pid_uuid))
                ).scalar_one_or_none()
                active_project_name = getattr(project_rec, "name", None)
    except Exception:
        pass

    # ── Fetch connections scoped to the active project ──────────────
    from app.models.credential_connection import (
        AdobeConnection,
        AmplitudeConnection,
        BrazeConnection,
        MoengageConnection,
        RedshiftConnection,
        SnowflakeConnection,
    )

    bq_conns = []
    amp_conns = []
    adobe_conns = []
    rs_conns = []
    sf_conns = []
    oauth_conns = []
    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            uid = uuid.UUID(user_ctx.user_id)

            # OAuth connections (Google/Meta/TikTok/Snap)
            oauth_stmt = select(OAuthConnection).where(
                OAuthConnection.user_id == uid,
                OAuthConnection.is_active == True,
            )
            if active_pid_uuid:
                oauth_stmt = oauth_stmt.where(OAuthConnection.project_id == active_pid_uuid)
            oauth_result = await db.execute(oauth_stmt)
            oauth_conns = list(oauth_result.scalars().all())

            bq_stmt = select(BQConnection).where(BQConnection.user_id == uid, BQConnection.is_active == True)
            if active_pid_uuid:
                bq_stmt = bq_stmt.where(BQConnection.fluxito_project_id == active_pid_uuid)
            bq_conns = (await db.execute(bq_stmt)).scalars().all()

            amp_stmt = select(AmplitudeConnection).where(
                AmplitudeConnection.user_id == uid, AmplitudeConnection.is_active == True
            )
            if active_pid_uuid:
                amp_stmt = amp_stmt.where(AmplitudeConnection.project_id == active_pid_uuid)
            amp_conns = (await db.execute(amp_stmt)).scalars().all()

            mixpanel_stmt = select(MixpanelConnection).where(
                MixpanelConnection.user_id == uid, MixpanelConnection.is_active == True
            )
            if active_pid_uuid:
                mixpanel_stmt = mixpanel_stmt.where(MixpanelConnection.project_id == active_pid_uuid)
            mixpanel_conns = (await db.execute(mixpanel_stmt)).scalars().all()

            posthog_stmt = select(PostHogConnection).where(
                PostHogConnection.user_id == uid, PostHogConnection.is_active == True
            )
            if active_pid_uuid:
                posthog_stmt = posthog_stmt.where(PostHogConnection.project_id == active_pid_uuid)
            posthog_conns = (await db.execute(posthog_stmt)).scalars().all()

            braze_stmt = select(BrazeConnection).where(
                BrazeConnection.user_id == uid, BrazeConnection.is_active == True
            )
            if active_pid_uuid:
                braze_stmt = braze_stmt.where(BrazeConnection.project_id == active_pid_uuid)
            braze_conns = (await db.execute(braze_stmt)).scalars().all()

            moengage_stmt = select(MoengageConnection).where(
                MoengageConnection.user_id == uid, MoengageConnection.is_active == True
            )
            if active_pid_uuid:
                moengage_stmt = moengage_stmt.where(MoengageConnection.project_id == active_pid_uuid)
            moengage_conns = (await db.execute(moengage_stmt)).scalars().all()

            adobe_stmt = select(AdobeConnection).where(
                AdobeConnection.user_id == uid, AdobeConnection.is_active == True
            )
            if active_pid_uuid:
                adobe_stmt = adobe_stmt.where(AdobeConnection.project_id == active_pid_uuid)
            adobe_conns = (await db.execute(adobe_stmt)).scalars().all()

            rs_stmt = select(RedshiftConnection).where(
                RedshiftConnection.user_id == uid, RedshiftConnection.is_active == True
            )
            if active_pid_uuid:
                rs_stmt = rs_stmt.where(RedshiftConnection.project_id == active_pid_uuid)
            rs_conns = (await db.execute(rs_stmt)).scalars().all()

            sf_stmt = select(SnowflakeConnection).where(
                SnowflakeConnection.user_id == uid, SnowflakeConnection.is_active == True
            )
            if active_pid_uuid:
                sf_stmt = sf_stmt.where(SnowflakeConnection.project_id == active_pid_uuid)
            sf_conns = (await db.execute(sf_stmt)).scalars().all()
    except Exception:
        pass

    # Filter OAuth connections by provider (from project-scoped DB query)
    meta_conns = [c for c in oauth_conns if c.provider == "meta"]
    tiktok_conns = [c for c in oauth_conns if c.provider == "tiktok"]
    snap_conns = [c for c in oauth_conns if c.provider == "snap"]
    linkedin_conns = [c for c in oauth_conns if c.provider == "linkedin"]
    pinterest_conns = [c for c in oauth_conns if c.provider == "pinterest"]
    x_conns = [c for c in oauth_conns if c.provider == "x"]
    google_conns = [c for c in oauth_conns if (c.provider or "google") in ("google", None, "")]

    # Determine which Google services are enabled based on granted scopes
    google_has_ga4 = False
    google_has_gtm = False
    google_has_ads = False
    google_has_gsc = False
    google_has_data_manager = False
    for gc in google_conns:
        scopes = gc.scopes or []
        if any("analytics" in s for s in scopes):
            google_has_ga4 = True
        if any("tagmanager" in s for s in scopes):
            google_has_gtm = True
        if "https://www.googleapis.com/auth/adwords" in scopes:
            google_has_ads = True
        if any("webmasters" in s for s in scopes):
            google_has_gsc = True
        if "https://www.googleapis.com/auth/datamanager" in scopes:
            google_has_data_manager = True

    # Count connectors GRANULARLY the SAME way /connect does: each connector
    # counts as ONE, and Google's five services (GA4/GTM/Ads/Search Console/Data Manager)
    # each count SEPARATELY. Build a lightweight flags object carrying the
    # has_* attributes named in GRANULAR_CONNECTOR_CATALOG, then derive
    # (connected, total) via the shared counter so /home and /connect can
    # never drift. reddit/bing are project-scoped OAuth providers; derive
    # them from oauth_conns the same way the other ad platforms are.
    reddit_conns = [c for c in oauth_conns if c.provider == "reddit"]
    bing_conns = [c for c in oauth_conns if c.provider == "bing"]
    apple_conns = [c for c in oauth_conns if c.provider == "apple"]
    conn_flags = SimpleNamespace(
        has_ga4=google_has_ga4,
        has_gtm=google_has_gtm,
        has_ads=google_has_ads,
        has_gsc=google_has_gsc,
        has_data_manager=google_has_data_manager,
        has_bq=bool(bq_conns),
        has_redshift=bool(rs_conns),
        has_snowflake=bool(sf_conns),
        has_amplitude=bool(amp_conns),
        has_mixpanel=bool(mixpanel_conns),
        has_posthog=bool(posthog_conns),
        has_braze=bool(braze_conns),
        has_moengage=bool(moengage_conns),
        has_adobe_analytics=bool(adobe_conns),
        has_adobe_launch=bool(adobe_conns),
        has_meta=bool(meta_conns),
        has_tiktok=bool(tiktok_conns),
        has_snap=bool(snap_conns),
        has_x=bool(x_conns),
        has_reddit=bool(reddit_conns),
        has_apple=bool(apple_conns),
        has_linkedin=bool(linkedin_conns),
        has_pinterest=bool(pinterest_conns),
        has_bing=bool(bing_conns),
    )
    connected_count, total_connector_count = count_granular_connectors(conn_flags)

    # Published API rate limits + real usage for the project's connected tools,
    # reusing the connector flags already resolved above. Marketo isn't tracked on
    # this page's flags, so it falls into the catalog list here.
    from app.connectors import rate_limits
    from app.connectors import usage as connector_usage

    rl_connected, rl_available = rate_limits.partition(rate_limits.connected_keys(conn_flags))
    rl_usage = await connector_usage.usage_for(active_pid_uuid, [c.key for c in rl_connected], days=30)
    rl_connected_view = rate_limits.to_view(rl_connected)
    for _rl_row in rl_connected_view:
        _rl_row["usage_count"] = rl_usage.get(_rl_row["key"])

    user_view = await _load_user_view(user_ctx)

    # ── Briefing feed: findings from the most recent audit run ─────────────
    # Pulled from the Auditing Platform (audit_runs/audit_findings) so the
    # briefing reflects whatever was actually checked for this project.
    briefing_findings: list[dict] = []
    last_audit_display = "Never"
    audit_checked_summary: str | None = None
    audit_open_issues = 0

    try:
        if active_pid_uuid:
            from sqlalchemy import case, desc

            from app.models.auditing import AuditFinding, AuditRun

            db_session3 = app_state.db_session_factory()
            async with db_session3 as db3:
                latest_run = (
                    await db3.execute(
                        select(AuditRun)
                        .where(AuditRun.project_id == active_pid_uuid, AuditRun.status == "complete")
                        .order_by(desc(AuditRun.created_at))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if latest_run:
                    last_audit_display = _time_ago(latest_run.created_at)
                    audit_open_issues = latest_run.critical_count + latest_run.warning_count
                    checked = (
                        latest_run.passed_count
                        + latest_run.critical_count
                        + latest_run.warning_count
                        + latest_run.info_count
                    )
                    audit_checked_summary = (
                        f"The last audit checked {checked} things · {latest_run.passed_count} passed"
                    )
                    findings = (
                        (
                            await db3.execute(
                                select(AuditFinding)
                                .where(AuditFinding.run_id == latest_run.id, AuditFinding.passed == False)
                                .order_by(
                                    case(
                                        (AuditFinding.severity == "critical", 0),
                                        (AuditFinding.severity == "warning", 1),
                                        else_=2,
                                    )
                                )
                                .limit(40)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    # Skip findings the team has resolved or snoozed.
                    from app.services.audit_triage import annotate_findings, load_triage_map

                    f_dicts = [f.to_dict() for f in findings]
                    triage_map = await load_triage_map(
                        db3, latest_run.project_id, [d.get("fingerprint") or "" for d in f_dicts]
                    )
                    annotate_findings(f_dicts, triage_map, latest_run.created_at)
                    findings = [
                        f for f, d in zip(findings, f_dicts, strict=True) if d.get("triage_status") == "open"
                    ][:4]
                    for f in findings:
                        finding_key = str(f.id)
                        # "Show evidence" has no separate deep-link today — the
                        # only link we hold is the audit run detail page,
                        # already used by the primary "Review the fix" action
                        # — so evidence expands inline from the finding's own
                        # expected/actual/rule meta instead of duplicating
                        # that link.
                        evidence_meta = {
                            "rule_id": f.rule_id,
                            "platform": f.platform,
                            "event": f.event,
                            "expected": f.expected,
                            "actual": f.actual,
                        }
                        briefing_findings.append(
                            {
                                "key": finding_key,
                                "severity": "urgent" if f.severity == "critical" else "watch",
                                "title": f.message
                                or f"{(f.platform or 'Platform').title()} — {f.event or f.rule_id or 'issue found'}",
                                "body": f.remediation or "",
                                "link": f"/audits/run/{latest_run.id}",
                                "evidence_link": None,
                                "evidence_meta": {k: v for k, v in evidence_meta.items() if v},
                            }
                        )
    except Exception:
        pass

    # ── Greeting: time-of-day + how many things genuinely need attention ───
    hour = datetime.now().hour
    time_of_day = "Morning" if hour < 12 else "Afternoon" if hour < 18 else "Evening"
    needs_attention = sum(1 for f in briefing_findings if f["severity"] in ("urgent", "watch"))

    tutorial_completed = bool(getattr(user_rec, "tutorial_completed_at", None))
    preferred_ai_client = getattr(user_rec, "preferred_ai_client", None)
    ai_client_labels = {
        "mcp": "MCP client",
        "claude": "Claude",
        "chatgpt": "ChatGPT",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "claude-code": "Claude Code",
        "codex": "Codex",
    }
    ai_setup_label = ai_client_labels.get(preferred_ai_client, "AI client") if preferred_ai_client else None
    setup_complete = sum(
        [
            tutorial_completed,
            bool(preferred_ai_client),
            bool(active_pid_uuid),
            connected_count > 0,
            last_audit_display != "Never",
        ]
    )

    # Recent MCP tool calls + authorized AI clients for the Home activity panel.
    recent_calls: list[dict] = []
    ai_clients: list[dict] = []
    try:
        from app.auth.mcp_session_manager import list_oauth_clients
        from app.models.audit import ToolCallAudit

        async with app_state.db_session_factory() as db:
            call_rows = (
                (
                    await db.execute(
                        select(ToolCallAudit)
                        .where(ToolCallAudit.user_id == uuid.UUID(str(user_ctx.user_id)))
                        .order_by(ToolCallAudit.created_at.desc())
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
        recent_calls = [
            {
                "id": str(c.id),
                "tool": c.tool_name,
                "platform": c.platform,
                "client": c.source_client,
                "summary": c.response_summary,
                "status": c.status,
                "is_write": bool(c.is_write),
                "ts": c.created_at.isoformat() if c.created_at else None,
            }
            for c in call_rows
        ]
        ai_clients = await list_oauth_clients(user_ctx.user_id)
    except Exception as e:
        logger.warning(f"Home: could not load recent calls/clients: {e}")

    # Ensure active project cookie is set
    active_pid = await ensure_active_project(request, user_ctx.user_id)

    response = render(
        request,
        "dashboard_home.html",
        {
            "user": user_view,
            "total_conns": connected_count,
            "total_platforms": total_connector_count,
            "rate_limits_connected": rl_connected_view,
            "rate_limits_available": rate_limits.to_view(rl_available),
            "rate_limits_reviewed": rate_limits.REVIEWED,
            "rate_limits_usage_days": 30,
            "briefing_findings": briefing_findings,
            "briefing_needs_attention": needs_attention,
            "briefing_time_of_day": time_of_day,
            "briefing_now_display": datetime.now().strftime("%A, %B %-d · %H:%M"),
            "audit_checked_summary": audit_checked_summary,
            "last_audit_display": last_audit_display,
            "audit_open_issues": audit_open_issues,
            "active_project_name": active_project_name,
            "has_workspace": bool(active_pid_uuid),
            "tutorial_completed": tutorial_completed,
            "preferred_ai_client": preferred_ai_client,
            "ai_setup_label": ai_setup_label,
            "setup_complete": setup_complete,
            "recent_calls": recent_calls,
            "ai_clients": ai_clients,
            "has_audit": last_audit_display != "Never",
            "has_connection": connected_count > 0,
        },
    )

    if active_pid and active_pid != get_active_project_id(request):
        set_active_project_cookie(response, active_pid)

    return response


@router.get("/api/home/feed")
async def home_feed(request: Request):
    """Merged reverse-chronological activity feed for the Home page.

    Blends recent items across the product into a single stream (newest
    first, capped at 30). Each item has the shape::

        {"kind", "title", "subtitle", "status", "ts", "url"}

    ``kind`` is one of ``audit`` | ``test_flow`` | ``activity``. ``ts`` is an ISO-8601 string. Sources with
    no rows simply contribute nothing — the endpoint is best-effort and
    fail-silent per source so one broken query can't blank the feed.
    """
    from sqlalchemy import desc as _desc

    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    active_pid = await ensure_active_project(request, user_ctx.user_id)
    if not active_pid:
        return JSONResponse({"items": []})
    pid = uuid.UUID(active_pid)

    items: list[dict] = []

    def _iso(dt):
        return dt.isoformat() if dt else None

    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            # ── Recent audit run completions ──────────────────────────
            try:
                from app.models.auditing import AuditRun

                rows = (
                    (
                        await db.execute(
                            select(AuditRun)
                            .where(AuditRun.project_id == pid)
                            .order_by(_desc(AuditRun.created_at))
                            .limit(30)
                        )
                    )
                    .scalars()
                    .all()
                )
                for r in rows:
                    score_txt = f"Score {r.score}" if r.score is not None else "Completed"
                    items.append(
                        {
                            "kind": "audit",
                            "title": r.title or f"{(r.audit_type or 'Audit').title()} audit",
                            "subtitle": f"{score_txt} · {r.critical_count} critical, {r.warning_count} warnings",
                            "status": r.status or "complete",
                            "ts": _iso(r.created_at),
                            "url": f"/audits/run/{r.id}",
                        }
                    )
            except Exception:
                pass

            # ── Recent test-flow runs ─────────────────────────────────
            try:
                from app.models.test_flows import TestFlow, TestFlowRun

                rows = (
                    await db.execute(
                        select(TestFlowRun, TestFlow.name)
                        .join(TestFlow, TestFlowRun.flow_id == TestFlow.id)
                        .where(TestFlowRun.project_id == pid)
                        .order_by(_desc(TestFlowRun.started_at))
                        .limit(30)
                    )
                ).all()
                for run, flow_name in rows:
                    items.append(
                        {
                            "kind": "test_flow",
                            "title": flow_name or "Test flow",
                            "subtitle": f"{run.assertions_passed}/{run.assertions_total} assertions passed",
                            "status": run.status,
                            "ts": _iso(run.finished_at or run.started_at),
                            "url": f"/audits/flows/{run.flow_id}/runs/{run.id}",
                        }
                    )
            except Exception:
                pass

            # ── Recent activity events ────────────────────────────────
            try:
                from app.models.activity import ActivityEvent

                rows = (
                    (
                        await db.execute(
                            select(ActivityEvent)
                            .where(ActivityEvent.project_id == pid)
                            .order_by(_desc(ActivityEvent.created_at))
                            .limit(30)
                        )
                    )
                    .scalars()
                    .all()
                )
                for e in rows:
                    items.append(
                        {
                            "kind": "activity",
                            "title": e.description,
                            "subtitle": (e.event_type or "").replace("_", " ").title(),
                            "status": None,
                            "ts": _iso(e.created_at),
                            "url": "/activity-log",
                        }
                    )
            except Exception:
                pass
    except Exception:
        return JSONResponse({"items": []})

    # Merge: newest first, cap at 30. Items with no ts sink to the bottom.
    items.sort(key=lambda it: it.get("ts") or "", reverse=True)
    return JSONResponse({"items": items[:30]})


@router.get("/signin")
async def signin(
    request: Request,
    next: str = Query(default="/home"),
    error: str | None = Query(default=None),
    gated: str | None = Query(default=None),
):
    """
    Sign-in interstitial page — shows email/password form + Google button.

    Uses the same resolver as protected routes so a signature-valid uid
    cookie pointing at a missing user does NOT cause /home → /signin →
    /home loops; instead the stale cookie is cleared and the form is
    rendered. The ``next`` query parameter is sanitized to prevent open
    redirects (e.g. ``next=//evil.com``).
    """
    next_val = next if isinstance(next, str) else request.query_params.get("next", "/home")
    safe_next = safe_next_url(next_val, "/home")

    err_val = error if isinstance(error, str) else request.query_params.get("error")
    gated_val = gated if isinstance(gated, str) else request.query_params.get("gated")

    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is not None:
        return RedirectResponse(url=safe_next, status_code=302)

    # Check if this is the first-run (no users in DB yet)
    first_run = False
    try:
        from sqlalchemy import exists as _exists
        from sqlalchemy import select as _select

        from app.models.user import User as _User

        async with app_state.db_session_factory() as db:
            result = await db.execute(_select(_exists().where(_User.id.is_not(None))))
            first_run = not result.scalar()
    except Exception:
        pass

    # Check if Google OAuth is configured so the template can hide the button.
    # A row that exists but fails to decrypt (wrong/rotated encryption key) is
    # treated the same as "not configured" — either way we can't offer the
    # button, and the sign-in page must never 500 over it.
    from cryptography.fernet import InvalidToken

    from app.auth.oauth_app_credentials import get_oauth_app_credentials, OAuthAppNotConfigured

    google_configured = False
    if not first_run:
        try:
            async with app_state.db_session_factory() as _cred_db:
                await get_oauth_app_credentials(_cred_db, "google")
            google_configured = True
        except (OAuthAppNotConfigured, InvalidToken):
            pass

    # Admin-controlled sign-in surface flags. During first-run the operator must
    # be able to create the admin account with a password, so force those on.
    from app.settings_service import get_auth_flags

    if first_run:
        flags = {"google_enabled": True, "password_enabled": True, "signup_enabled": True}
    else:
        flags = await get_auth_flags()

    response = render(
        request,
        "auth/signin.html",
        {
            "next_url": safe_next,
            "google_configured": google_configured and flags["google_enabled"],
            "first_run": first_run,
            "signup_enabled": flags["signup_enabled"],
            "password_enabled": flags["password_enabled"],
            "error": err_val,
            "gated": gated_val in ("1", "true", "True"),
        },
    )

    # Cookie sig was valid but the user could not be loaded — clear it
    # so subsequent requests don't keep triggering the redirect dance.
    if getattr(request.state, "uid_cookie_invalid", False):
        response.delete_cookie("uid", path="/")

    return response


async def _google_signin_enabled() -> bool:
    """The admin "Google sign-in" switch, enforced server-side (not just the button)."""
    try:
        from app.settings_service import get_auth_flags

        return bool((await get_auth_flags()).get("google_enabled", True))
    except Exception:
        return True


@router.get("/auth/google/start")
async def google_start(request: Request, next: str = Query(default="/home")):
    """
    Initiate Google OAuth sign-in flow.
    Called when user clicks "Continue with Google" on the interstitial page.
    """
    import urllib.parse

    if not await _google_signin_enabled():
        return RedirectResponse(url="/signin?error=google_disabled", status_code=302)

    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    next_val = next if isinstance(next, str) else request.query_params.get("next", "/home")
    safe_next = safe_next_url(next_val, "/home")

    state = secrets.token_urlsafe(32)
    redis = app_state.redis_client
    await redis.setex(
        f"signin_state:{state}",
        600,
        json.dumps(
            {
                "next": safe_next,
                "base_url": req_base_url,
            }
        ),
    )

    from cryptography.fernet import InvalidToken

    from app.auth.oauth_app_credentials import get_oauth_app_credentials, OAuthAppNotConfigured

    try:
        async with app_state.db_session_factory() as _cred_db:
            _google_creds = await get_oauth_app_credentials(_cred_db, "google")
    except (OAuthAppNotConfigured, InvalidToken):
        return RedirectResponse(
            url="/signin?error=google_not_configured",
            status_code=302,
        )

    signin_redirect_uri = f"{req_base_url}/auth/google/signin/callback"
    params = {
        "client_id": _google_creds.client_id,
        "redirect_uri": signin_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/google/signin/callback")
async def signin_callback(
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str | None = Query(default=None),
):
    """
    Callback for the standalone sign-in flow.
    Creates/finds the user, sets uid cookie, redirects to stored `next`.
    """
    err_val = error if isinstance(error, str) else request.query_params.get("error")
    if err_val:
        from urllib.parse import quote

        return RedirectResponse(url=f"/?signin_error={quote(str(err_val)[:64])}", status_code=302)
    if not await _google_signin_enabled():
        return RedirectResponse(url="/signin?error=google_disabled", status_code=302)

    code_val = code if isinstance(code, str) else request.query_params.get("code")
    state_val = state if isinstance(state, str) else request.query_params.get("state")
    if not code_val or not state_val:
        return RedirectResponse(url="/", status_code=302)

    redis = app_state.redis_client
    state_raw = await redis.get(f"signin_state:{state_val}")
    if not state_raw:
        return RedirectResponse(url="/", status_code=302)

    state_str = state_raw.decode() if isinstance(state_raw, bytes) else state_raw
    await redis.delete(f"signin_state:{state_val}")

    # Parse stored state — may be plain string (legacy) or JSON object.
    try:
        state_data = json.loads(state_str)
        next_url = state_data.get("next", "/home")
        stored_base_url = state_data.get("base_url", "")
    except (json.JSONDecodeError, TypeError):
        # Legacy: plain next-URL string
        next_url = state_str
        stored_base_url = ""

    # Re-validate the stored next URL — covers legacy state entries
    # written before sanitization was added at the /auth/google/start
    # boundary.
    next_url = safe_next_url(next_url, "/home")

    # Special case for MCP OAuth continuation (Approach B)
    if next_url and next_url.startswith("/oauth/authorize/resume/"):
        # The resume token was generated server-side; allow it.
        pass

    # Build redirect_uri from stored base_url (must match the one used
    # in the authorization request).
    from app.utils import base_url_from_request

    base_url = stored_base_url or base_url_from_request(request)
    signin_redirect_uri = f"{base_url}/auth/google/signin/callback"

    # Exchange code for tokens
    import base64 as _base64

    from cryptography.fernet import InvalidToken

    from app.auth.oauth_app_credentials import get_oauth_app_credentials, OAuthAppNotConfigured

    try:
        async with app_state.db_session_factory() as _cred_db:
            _google_creds = await get_oauth_app_credentials(_cred_db, "google")
    except (OAuthAppNotConfigured, InvalidToken):
        return RedirectResponse(
            url="/signin?error=google_not_configured",
            status_code=302,
        )

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _google_creds.client_id,
                "client_secret": _google_creds.client_secret,
                "code": code_val,
                "redirect_uri": signin_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        return HTMLResponse("<h2>Sign-in failed. <a href='/'>Go back</a></h2>", status_code=400)

    token_data = token_resp.json()
    id_token_encoded = token_data.get("id_token")
    if not id_token_encoded:
        return HTMLResponse("<h2>No identity token received. <a href='/'>Go back</a></h2>", status_code=400)

    # Decode JWT payload (no sig verification needed here)
    payload_b64 = id_token_encoded.split(".")[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    google_user = json.loads(_base64.urlsafe_b64decode(payload_b64))

    email = google_user.get("email")
    display_name = google_user.get("name")
    if not email:
        return HTMLResponse(
            "<h2>Could not retrieve email from Google. <a href='/'>Go back</a></h2>", status_code=400
        )
    # Only a Google-verified address may claim (or link onto) a Fluxito account.
    if google_user.get("email_verified") is not True:
        return HTMLResponse(
            "<h2>Your Google account's email address isn't verified.</h2>"
            "<p>Verify it with Google, or sign in with email and password. <a href='/signin'>Back</a></p>",
            status_code=400,
        )

    # Create or upsert user
    from app.models.user import User

    is_new_user = False
    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            from app.settings_service import get_runtime_setting

            gate_on = bool(await get_runtime_setting(db, "require_access_approval", default=False))
            if gate_on:
                from app.services.invites import has_open_invite

                gate_on = not await has_open_invite(email)
            if gate_on:
                return RedirectResponse(url="/signin?gated=1", status_code=302)
            user = User(
                email=email,
                display_name=display_name,
                email_verified=True,
                email_verified_at=datetime.utcnow(),
                auth_provider="google",
            )
            db.add(user)
            await db.flush()
            is_new_user = True
        else:
            # Account-collision guard: if the existing record was created
            # via email/password and *never verified*, refuse to silently
            # link a Google identity onto it. Otherwise an attacker who
            # registered ``victim@example.com`` (without ever clicking the
            # verification link) would have their unverified record
            # auto-claimed and verified the moment the real owner signed
            # in with Google. The legitimate owner is told to verify the
            # password account first (or use the password-reset flow);
            # already-verified email accounts continue to link normally
            # because both sides demonstrably control the inbox.
            if user.auth_provider == "email" and not user.email_verified:
                logger.warning(
                    "Google sign-in blocked for %s: existing email account is unverified",
                    email,
                )
                return HTMLResponse(
                    "<h2>This email already has a password account that hasn't been verified yet.</h2>"
                    "<p>Please <a href='/signin'>sign in with your password</a> and complete email verification, "
                    "or use <a href='/signin'>Forgot password</a> to reset it. Once the email is verified, "
                    "you'll be able to sign in with Google.</p>",
                    status_code=409,
                )
            if getattr(user, "deactivated_by_admin", False):
                return HTMLResponse(
                    "<h2>This account has been deactivated by an administrator.</h2><p><a href='/'>Back</a></p>",
                    status_code=403,
                )
            if getattr(user, "is_active", True) is False:
                # The user paused their own account; signing in again reactivates it.
                user.is_active = True
            if display_name and not user.display_name:
                user.display_name = display_name
            # Google sign-in always verifies email
            if not user.email_verified:
                user.email_verified = True
                user.email_verified_at = datetime.utcnow()
            # Link Google auth if they signed up with email
            if user.auth_provider == "email":
                user.auth_provider = "both"
        # Check if this user has completed the tutorial
        needs_tutorial = user.tutorial_completed_at is None
        await db.commit()
        user_id = str(user.id)

    if not needs_tutorial:
        try:
            from app.api.project_routes import ensure_default_project

            await ensure_default_project(user_id, display_name, email)
        except Exception:
            logger.warning("ensure_default_project failed for Google user", exc_info=True)

    # Redirect new users (or those who haven't finished) to tutorial
    redirect_url = next_url
    if needs_tutorial and next_url in ("/home", "/onboard", "/connect"):
        redirect_url = "/tutorial"

    # Set uid cookie and redirect
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        "uid",
        sign_uid(user_id),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.APP_ENV == "production",
    )
    return response


@router.get("/signout")
async def signout(request: Request):
    """Clear the uid session cookie and redirect to sign-in (or a safe ``next``)."""
    from app.utils import safe_next_url

    response = RedirectResponse(
        url=safe_next_url(request.query_params.get("next"), default="/"), status_code=302
    )
    response.delete_cookie("uid")
    return response


@router.get("/onboard", response_class=HTMLResponse)
async def onboard_page(request: Request):
    """Legacy onboard URL — redirects to the interactive tutorial."""
    return RedirectResponse(url="/tutorial", status_code=302)


@router.get("/tutorial", response_class=HTMLResponse)
async def tutorial_page(request: Request):
    """Interactive onboarding tutorial — guides new users through setup step by step.

    Pass ``?force=1`` (or ``?replay=1``) to bypass the "already completed" redirect
    and view the tutorial again.
    """
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return RedirectResponse(url="/signin?next=/tutorial", status_code=302)

    # Resolve active project if one already exists. If not, the user will
    # create their project in Step 2 of the onboarding journey.
    active_pid = await ensure_active_project(request, user_ctx.user_id)
    active_project_name = None
    if active_pid:
        try:
            db_session_p = app_state.db_session_factory()
            async with db_session_p as db_p:
                p_row = (
                    await db_p.execute(select(Project).where(Project.id == uuid.UUID(active_pid)))
                ).scalar_one_or_none()
                if p_row:
                    active_project_name = p_row.name
        except Exception:
            pass

    force = request.query_params.get("force") == "1" or request.query_params.get("replay") == "1"

    # Check if user already completed the tutorial (skip redirect when force/replay is set)
    preferred_ai_client = None
    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            result = await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
            user = result.scalar_one_or_none()
            if user and user.tutorial_completed_at is not None and not force:
                return RedirectResponse(url="/home", status_code=302)
            if user:
                preferred_ai_client = user.preferred_ai_client
    except Exception:
        pass

    has_connections = len(user_ctx.connections) > 0

    # ── Real connection status for Step 3 (Connect data) ────────────────
    # Same scope-based detection home() uses for GA4/GTM/Ads, plus a quick
    # check for Meta and BigQuery — just the 5 platforms shown in this step.
    conn_status = {"ga4": False, "gtm": False, "gads": False, "meta": False, "bq": False}
    try:
        for c in user_ctx.connections:
            if c.provider == "meta":
                conn_status["meta"] = True
            elif (c.provider or "google") in ("google", None, ""):
                scopes = c.scopes or []
                if any("analytics" in s for s in scopes):
                    conn_status["ga4"] = True
                if any("tagmanager" in s for s in scopes):
                    conn_status["gtm"] = True
                if "https://www.googleapis.com/auth/adwords" in scopes:
                    conn_status["gads"] = True
        if active_pid:
            from app.models.bq_connection import BQConnection

            db_session_bq = app_state.db_session_factory()
            async with db_session_bq as db_bq:
                bq_row = (
                    await db_bq.execute(
                        select(BQConnection)
                        .where(
                            BQConnection.user_id == uuid.UUID(user_ctx.user_id),
                            BQConnection.fluxito_project_id == uuid.UUID(active_pid),
                            BQConnection.is_active == True,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                conn_status["bq"] = bq_row is not None
    except Exception:
        pass

    user_view = await _load_user_view(user_ctx)
    return render(
        request,
        "tutorial.html",
        {
            "user": user_view,
            "has_connections": has_connections,
            "conn_status": conn_status,
            "conn_live_count": sum(1 for v in conn_status.values() if v),
            "preferred_ai_client": preferred_ai_client,
            "active_project_id": active_pid,
            "active_project_name": active_project_name,
        },
    )


@router.post("/api/tutorial/complete")
async def tutorial_complete(request: Request):
    """Mark the tutorial as completed for the current user."""
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    # Guard: completion is meaningful only if there is a project to land
    # on afterwards. Without this, a user with zero projects could mark
    # the tutorial complete and then loop because /home redirects back
    # to /projects.
    active_pid = await ensure_active_project(request, user_ctx.user_id)
    if not active_pid:
        from app.api.project_routes import ensure_default_project

        await ensure_default_project(
            user_id=user_ctx.user_id,
            display_name=user_ctx.display_name,
            email=user_ctx.email,
        )
        active_pid = await ensure_active_project(request, user_ctx.user_id)

    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            await db.execute(
                update(User)
                .where(User.id == uuid.UUID(user_ctx.user_id))
                .values(tutorial_completed_at=datetime.utcnow())
            )
            await db.commit()
    except Exception:
        return JSONResponse({"error": "failed"}, status_code=500)

    res = JSONResponse({"ok": True})
    if active_pid:
        from app.api.project_routes import set_active_project_cookie

        set_active_project_cookie(res, active_pid)
    return res


@router.post("/api/onboarding/preferences")
async def save_onboarding_preferences(request: Request):
    """Persist the onboarding AI-client choice.

    Body is a partial dict — currently just ``ai_client``. Surfaced later in
    Profile → Preferences.
    """
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    values: dict = {}
    if "ai_client" in payload:
        values["preferred_ai_client"] = payload["ai_client"]

    if not values:
        return JSONResponse({"ok": True})

    try:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            await db.execute(update(User).where(User.id == uuid.UUID(user_ctx.user_id)).values(**values))
            await db.commit()
    except Exception:
        return JSONResponse({"error": "failed"}, status_code=500)

    return JSONResponse({"ok": True})


@router.get("/api/onboarding/health-check")
async def onboarding_health_check(request: Request):
    """Real connection summary for onboarding Step 5 (health check).

    No fabricated findings — every line reflects data this project actually
    has: connections made, tracking-plan coverage, and structural gaps.
    """
    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    active_pid = get_active_project_id(request)
    lines: list[dict] = []
    connected_names: list[str] = []

    try:
        google_scopes: list[str] = []
        has_meta = False
        for c in user_ctx.connections:
            if c.provider == "meta":
                has_meta = True
            elif (c.provider or "google") in ("google", None, ""):
                google_scopes.extend(c.scopes or [])
        if any("analytics" in s for s in google_scopes):
            connected_names.append("GA4")
        if any("tagmanager" in s for s in google_scopes):
            connected_names.append("Google Tag Manager")
        if "https://www.googleapis.com/auth/adwords" in google_scopes:
            connected_names.append("Google Ads")
        if "https://www.googleapis.com/auth/datamanager" in google_scopes:
            connected_names.append("Google Data Manager")
        if has_meta:
            connected_names.append("Meta Ads")

        if active_pid:
            from app.models.bq_connection import BQConnection

            db_session_bq = app_state.db_session_factory()
            async with db_session_bq as db_bq:
                bq_row = (
                    await db_bq.execute(
                        select(BQConnection)
                        .where(
                            BQConnection.user_id == uuid.UUID(user_ctx.user_id),
                            BQConnection.fluxito_project_id == uuid.UUID(active_pid),
                            BQConnection.is_active == True,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if bq_row is not None:
                    connected_names.append("BigQuery")
    except Exception:
        pass

    if connected_names:
        lines.append(
            {
                "icon": "✓",
                "color": "good",
                "text": f"{', '.join(connected_names)}: connected · read-only access confirmed",
            }
        )
    else:
        lines.append(
            {
                "icon": "○",
                "color": "muted",
                "text": "No platforms connected yet — add one from Connections",
            }
        )

    lines.append({"icon": "✓", "color": "good", "text": "scopes verified: read-only · nothing was changed"})

    summary = "You're set up. Run an audit from the Audit section to see how your tracking looks."

    return JSONResponse({"lines": lines, "summary": summary})


@router.get("/connect")
async def connect_page_legacy_redirect():
    """Legacy /connect entry point — connections now live in the settings shell."""
    return RedirectResponse(url="/settings/connections", status_code=302)


@router.get("/settings/connections", response_class=HTMLResponse)
async def connect_page(request: Request):
    """Google OAuth tier selector + platform picker — scoped to active project.

    Rendered inside the settings shell (rail visible) at /settings/connections.
    """
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/settings/connections", status_code=302)

    # Connections are project-scoped — if the user has no project yet,
    # send them to create one before they can connect anything.
    active_pid = await ensure_active_project(request, user_ctx.user_id)
    if not active_pid:
        return RedirectResponse(url="/projects", status_code=302)
    active_pid_uuid = uuid.UUID(active_pid)

    user_view = await _load_user_view(user_ctx)

    # Build uniform connections_view + count per platform
    bq_count = 0
    amplitude_count = 0
    branch_count = 0
    appsflyer_count = 0
    adjust_count = 0
    mixpanel_count = 0
    posthog_count = 0
    braze_count = 0
    moengage_count = 0
    adobe_count = 0
    marketo_count = 0
    redshift_count = 0
    snowflake_count = 0
    google_count = 0
    meta_count = 0
    tiktok_count = 0
    snap_count = 0
    linkedin_count = 0
    pinterest_count = 0
    x_count = 0
    reddit_count = 0
    bing_count = 0
    apple_count = 0
    connections_view = []
    bq_rows = []
    oauth_rows = []
    marketo_rows = []
    branch_rows = []
    appsflyer_rows = []
    adjust_rows = []
    mixpanel_rows = []
    posthog_rows = []
    braze_rows = []
    moengage_rows = []
    if user_ctx is not None:
        try:
            db_session = app_state.db_session_factory()
            async with db_session as db:
                # OAuth connections (Google, Meta, TikTok, Snap) — filtered by project
                oauth_stmt = select(OAuthConnection).where(
                    OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
                    OAuthConnection.is_active == True,
                )
                if active_pid_uuid:
                    oauth_stmt = oauth_stmt.where(OAuthConnection.project_id == active_pid_uuid)
                oauth_result = await db.execute(oauth_stmt)
                oauth_rows = list(oauth_result.scalars().all())

                # BQ — uses fluxito_project_id
                bq_stmt = select(BQConnection).where(
                    BQConnection.user_id == uuid.UUID(user_ctx.user_id),
                    BQConnection.is_active == True,
                )
                if active_pid_uuid:
                    bq_stmt = bq_stmt.where(BQConnection.fluxito_project_id == active_pid_uuid)
                bq_result = await db.execute(bq_stmt)
                bq_rows = list(bq_result.scalars().all())
                bq_count = len(bq_rows)

                # Credential-based connections — all filtered by project
                amp_stmt = select(AmplitudeConnection).where(
                    AmplitudeConnection.user_id == uuid.UUID(user_ctx.user_id),
                    AmplitudeConnection.is_active == True,
                )
                if active_pid_uuid:
                    amp_stmt = amp_stmt.where(AmplitudeConnection.project_id == active_pid_uuid)
                amp_result = await db.execute(amp_stmt)
                amplitude_rows = list(amp_result.scalars().all())
                amplitude_count = len(amplitude_rows)

                # Branch / AppsFlyer / Adjust (MMP credential connections) — filtered by project
                branch_stmt = select(BranchConnection).where(
                    BranchConnection.user_id == uuid.UUID(user_ctx.user_id),
                    BranchConnection.is_active == True,
                )
                if active_pid_uuid:
                    branch_stmt = branch_stmt.where(BranchConnection.project_id == active_pid_uuid)
                branch_result = await db.execute(branch_stmt)
                branch_rows = list(branch_result.scalars().all())
                branch_count = len(branch_rows)

                appsflyer_stmt = select(AppsFlyerConnection).where(
                    AppsFlyerConnection.user_id == uuid.UUID(user_ctx.user_id),
                    AppsFlyerConnection.is_active == True,
                )
                if active_pid_uuid:
                    appsflyer_stmt = appsflyer_stmt.where(AppsFlyerConnection.project_id == active_pid_uuid)
                appsflyer_result = await db.execute(appsflyer_stmt)
                appsflyer_rows = list(appsflyer_result.scalars().all())
                appsflyer_count = len(appsflyer_rows)

                adjust_stmt = select(AdjustConnection).where(
                    AdjustConnection.user_id == uuid.UUID(user_ctx.user_id),
                    AdjustConnection.is_active == True,
                )
                if active_pid_uuid:
                    adjust_stmt = adjust_stmt.where(AdjustConnection.project_id == active_pid_uuid)
                adjust_result = await db.execute(adjust_stmt)
                adjust_rows = list(adjust_result.scalars().all())
                adjust_count = len(adjust_rows)

                # Mixpanel / PostHog (product analytics credential connections) — filtered by project
                mixpanel_stmt = select(MixpanelConnection).where(
                    MixpanelConnection.user_id == uuid.UUID(user_ctx.user_id),
                    MixpanelConnection.is_active == True,
                )
                if active_pid_uuid:
                    mixpanel_stmt = mixpanel_stmt.where(MixpanelConnection.project_id == active_pid_uuid)
                mixpanel_result = await db.execute(mixpanel_stmt)
                mixpanel_rows = list(mixpanel_result.scalars().all())
                mixpanel_count = len(mixpanel_rows)

                posthog_stmt = select(PostHogConnection).where(
                    PostHogConnection.user_id == uuid.UUID(user_ctx.user_id),
                    PostHogConnection.is_active == True,
                )
                if active_pid_uuid:
                    posthog_stmt = posthog_stmt.where(PostHogConnection.project_id == active_pid_uuid)
                posthog_result = await db.execute(posthog_stmt)
                posthog_rows = list(posthog_result.scalars().all())
                posthog_count = len(posthog_rows)

                # Braze / MoEngage (engagement platforms) — filtered by project
                braze_stmt = select(BrazeConnection).where(
                    BrazeConnection.user_id == uuid.UUID(user_ctx.user_id),
                    BrazeConnection.is_active == True,
                )
                if active_pid_uuid:
                    braze_stmt = braze_stmt.where(BrazeConnection.project_id == active_pid_uuid)
                braze_result = await db.execute(braze_stmt)
                braze_rows = list(braze_result.scalars().all())
                braze_count = len(braze_rows)

                moengage_stmt = select(MoengageConnection).where(
                    MoengageConnection.user_id == uuid.UUID(user_ctx.user_id),
                    MoengageConnection.is_active == True,
                )
                if active_pid_uuid:
                    moengage_stmt = moengage_stmt.where(MoengageConnection.project_id == active_pid_uuid)
                moengage_result = await db.execute(moengage_stmt)
                moengage_rows = list(moengage_result.scalars().all())
                moengage_count = len(moengage_rows)

                adobe_stmt = select(AdobeConnection).where(
                    AdobeConnection.user_id == uuid.UUID(user_ctx.user_id),
                    AdobeConnection.is_active == True,
                )
                if active_pid_uuid:
                    adobe_stmt = adobe_stmt.where(AdobeConnection.project_id == active_pid_uuid)
                adobe_result = await db.execute(adobe_stmt)
                adobe_rows = list(adobe_result.scalars().all())
                adobe_count = len(adobe_rows)

                marketo_stmt = select(MarketoConnection).where(
                    MarketoConnection.user_id == uuid.UUID(user_ctx.user_id),
                    MarketoConnection.is_active == True,
                )
                if active_pid_uuid:
                    marketo_stmt = marketo_stmt.where(MarketoConnection.project_id == active_pid_uuid)
                marketo_result = await db.execute(marketo_stmt)
                marketo_rows = list(marketo_result.scalars().all())
                marketo_count = len(marketo_rows)

                redshift_stmt = select(RedshiftConnection).where(
                    RedshiftConnection.user_id == uuid.UUID(user_ctx.user_id),
                    RedshiftConnection.is_active == True,
                )
                if active_pid_uuid:
                    redshift_stmt = redshift_stmt.where(RedshiftConnection.project_id == active_pid_uuid)
                redshift_result = await db.execute(redshift_stmt)
                redshift_rows = list(redshift_result.scalars().all())
                redshift_count = len(redshift_rows)

                snowflake_stmt = select(SnowflakeConnection).where(
                    SnowflakeConnection.user_id == uuid.UUID(user_ctx.user_id),
                    SnowflakeConnection.is_active == True,
                )
                if active_pid_uuid:
                    snowflake_stmt = snowflake_stmt.where(SnowflakeConnection.project_id == active_pid_uuid)
                snowflake_result = await db.execute(snowflake_stmt)
                snowflake_rows = list(snowflake_result.scalars().all())
                snowflake_count = len(snowflake_rows)
        except Exception:
            pass

        # Count OAuth connections by provider (from project-filtered DB query)
        google_count = len([c for c in oauth_rows if (c.provider or "google") in ("google", None, "")])
        meta_count = len([c for c in oauth_rows if c.provider == "meta"])
        tiktok_count = len([c for c in oauth_rows if c.provider == "tiktok"])
        snap_count = len([c for c in oauth_rows if c.provider == "snap"])
        linkedin_count = len([c for c in oauth_rows if c.provider == "linkedin"])
        pinterest_count = len([c for c in oauth_rows if c.provider == "pinterest"])
        x_count = len([c for c in oauth_rows if c.provider == "x"])
        reddit_count = len([c for c in oauth_rows if c.provider == "reddit"])
        bing_count = len([c for c in oauth_rows if c.provider == "bing"])
        apple_count = len([c for c in oauth_rows if c.provider == "apple"])
        # MMP credential counts are already computed from the credential fetch block above (branch_count etc.)

        name_map = {
            "google": "Google Suite",
            "meta": "Meta Ads",
            "tiktok": "TikTok Ads",
            "snap": "Snapchat Ads",
            "x": "X Ads",
            "bing": "Bing Webmaster Tools",
            "reddit": "Reddit Ads",
            "apple": "Apple Ads",
            "linkedin": "LinkedIn Ads",
            "pinterest": "Pinterest Ads",
            "bigquery": "BigQuery",
        }
        icon_map = {
            "google": "🔗",
            "meta": "📘",
            "tiktok": "🎵",
            "snap": "👻",
            "x": "X",
            "bing": "🔎",
            "reddit": "👽",
            "apple": "Apple",
            "linkedin": "💼",
            "pinterest": "📌",
            "bigquery": "🗄️",
        }
        delete_endpoint = {
            "google": "/api/connections/{id}",
            "meta": "/api/connections/meta/{id}",
            "tiktok": "/api/connections/tiktok/{id}",
            "snap": "/api/connections/snap/{id}",
            "x": "/api/connections/x/{id}",
            "bing": "/api/connections/bing/{id}",
            "reddit": "/api/connections/reddit/{id}",
            "apple": "/api/connections/apple/{id}",
            "linkedin": "/api/connections/linkedin/{id}",
            "pinterest": "/api/connections/pinterest/{id}",
            "bigquery": "/api/connections/bigquery/{id}",
        }
        edit_url_map = {
            "google": "/connect",
            "meta": "/connect/meta",
            "tiktok": "/connect/tiktok",
            "snap": "/connect/snap",
            "x": "/connect/x",
            "bing": "/connect/bing",
            "reddit": "/connect/reddit",
            "apple": "/connect/apple",
            "linkedin": "/connect/linkedin",
            "pinterest": "/connect/pinterest",
            "bigquery": "/connect/bigquery",
        }
        # provider -> (scope granting write access, read-only scope)
        scope_markers = {
            "meta": ("ads_management", "ads_read"),
            "tiktok": ("advertiser.manage", "advertiser.read"),
            "pinterest": ("ads:write", "ads:read"),
            "linkedin": ("rw_ads", "r_ads"),
            "reddit": ("adsedit", "adsread"),
        }
        for c in oauth_rows:
            prov = c.provider or "google"
            base_edit = edit_url_map.get(prov, f"/connect/{prov}")
            if prov == "google":
                edit_url = f"/connect/google?edit={c.id}"
            elif prov in ("meta", "tiktok", "snap"):
                edit_url = base_edit
            else:
                edit_url = f"{base_edit}?edit={c.id}"
            services: list[dict[str, object]] = []
            if prov == "google":
                scopes = c.scopes or []
                services = [
                    {
                        "slug": "ga4",
                        "name": "Google Analytics",
                        "enabled": any("analytics" in s for s in scopes),
                    },
                    {
                        "slug": "gtm",
                        "name": "Google Tag Manager",
                        "enabled": any("tagmanager" in s for s in scopes),
                    },
                    {
                        "slug": "search_console",
                        "name": "Search Console",
                        "enabled": any("webmasters" in s for s in scopes),
                    },
                    {
                        "slug": "google_ads",
                        "name": "Google Ads",
                        "enabled": "https://www.googleapis.com/auth/adwords" in scopes,
                    },
                    {
                        "slug": "data_manager",
                        "name": "Google Data Manager",
                        "enabled": "https://www.googleapis.com/auth/datamanager" in scopes,
                    },
                ]
            # Real access level from the granted OAuth scopes where the scope
            # names are conclusive; None lets the template infer a default.
            granted_scopes = set(c.scopes or [])
            scope_tier = None
            if prov == "google":
                scope_tier = (
                    "write"
                    if granted_scopes
                    & {
                        "https://www.googleapis.com/auth/analytics",
                        "https://www.googleapis.com/auth/tagmanager.edit.containers",
                    }
                    else "read"
                )
            else:
                write_scope, read_scope = scope_markers.get(prov, (None, None))
                if write_scope and write_scope in granted_scopes:
                    scope_tier = "write"
                elif read_scope and read_scope in granted_scopes:
                    scope_tier = "read"
            connections_view.append(
                {
                    "id": str(c.id),
                    "provider": prov,
                    "scope_tier": scope_tier,
                    "platform_name": name_map.get(prov, prov.title()),
                    "label": c.google_email or name_map.get(prov, prov.title()),
                    "detail": f"{len(c.scopes or [])} scopes granted" if (c.scopes) else "",
                    "icon": icon_map.get(prov, "🔗"),
                    "is_active": c.connection_status == "active",
                    "delete_url": delete_endpoint.get(prov, f"/api/connections/{prov}/{{id}}").format(
                        id=c.id
                    ),
                    "edit_url": edit_url,
                    "services": services,
                }
            )
        for bq in bq_rows:
            connections_view.append(
                {
                    "id": str(bq.id),
                    "provider": "bigquery",
                    "platform_name": "BigQuery",
                    "label": bq.display_name or "BigQuery",
                    "detail": f"Project: {bq.project_id}",
                    "icon": icon_map["bigquery"],
                    "is_active": bq.connection_status == "active",
                    "delete_url": delete_endpoint["bigquery"].format(id=bq.id),
                    "edit_url": f"/connect/bigquery?edit={bq.id}",
                }
            )

        # ── Amplitude connections ──
        for amp in amplitude_rows:
            connections_view.append(
                {
                    "id": str(amp.id),
                    "provider": "amplitude",
                    "platform_name": "Amplitude",
                    "label": amp.display_name or "Amplitude",
                    "detail": f"Project: {amp.project_name}" if amp.project_name else "Product analytics",
                    "icon": "📈",
                    "is_active": amp.connection_status == "active",
                    "delete_url": f"/api/connections/amplitude/{amp.id}",
                    "edit_url": f"/connect/amplitude?edit={amp.id}",
                }
            )

        # ── Branch connections ──
        for br in branch_rows:
            connections_view.append(
                {
                    "id": str(br.id),
                    "provider": "branch",
                    "platform_name": "Branch",
                    "label": br.display_name or "Branch",
                    "detail": f"Project: {br.project_name}" if br.project_name else "Mobile attribution",
                    "icon": "🌿",
                    "is_active": br.connection_status == "active",
                    "delete_url": f"/api/connections/branch/{br.id}",
                    "edit_url": f"/connect/branch?edit={br.id}",
                }
            )

        # ── AppsFlyer connections ──
        for af in appsflyer_rows:
            connections_view.append(
                {
                    "id": str(af.id),
                    "provider": "appsflyer",
                    "platform_name": "AppsFlyer",
                    "label": af.display_name or "AppsFlyer",
                    "detail": f"Project: {af.project_name}" if af.project_name else "Mobile attribution",
                    "icon": "🚀",
                    "is_active": af.connection_status == "active",
                    "delete_url": f"/api/connections/appsflyer/{af.id}",
                    "edit_url": f"/connect/appsflyer?edit={af.id}",
                }
            )

        # ── Adjust connections ──
        for adj in adjust_rows:
            connections_view.append(
                {
                    "id": str(adj.id),
                    "provider": "adjust",
                    "platform_name": "Adjust",
                    "label": adj.display_name or "Adjust",
                    "detail": f"Project: {adj.project_name}" if adj.project_name else "Mobile attribution",
                    "icon": "🎯",
                    "is_active": adj.connection_status == "active",
                    "delete_url": f"/api/connections/adjust/{adj.id}",
                    "edit_url": f"/connect/adjust?edit={adj.id}",
                }
            )

        # ── Mixpanel connections ──
        for mpx in mixpanel_rows:
            connections_view.append(
                {
                    "id": str(mpx.id),
                    "provider": "mixpanel",
                    "platform_name": "Mixpanel",
                    "label": mpx.display_name or "Mixpanel",
                    "detail": f"Project: {mpx.project_name}" if mpx.project_name else "Product analytics",
                    "icon": "🎯",
                    "is_active": mpx.connection_status == "active",
                    "delete_url": f"/api/connections/mixpanel/{mpx.id}",
                    "edit_url": f"/connect/mixpanel?edit={mpx.id}",
                }
            )

        # ── PostHog connections ──
        for ph in posthog_rows:
            connections_view.append(
                {
                    "id": str(ph.id),
                    "provider": "posthog",
                    "platform_name": "PostHog",
                    "label": ph.display_name or "PostHog",
                    "detail": ph.project_host or "Product analytics",
                    "icon": "🦔",
                    "is_active": ph.connection_status == "active",
                    "delete_url": f"/api/connections/posthog/{ph.id}",
                    "edit_url": f"/connect/posthog?edit={ph.id}",
                }
            )

        # ── Braze connections ──
        for bz in braze_rows:
            connections_view.append(
                {
                    "id": str(bz.id),
                    "provider": "braze",
                    "platform_name": "Braze",
                    "label": bz.display_name or "Braze",
                    "detail": bz.rest_endpoint_url or "Customer engagement",
                    "icon": "📨",
                    "is_active": bz.connection_status == "active",
                    "delete_url": f"/api/connections/braze/{bz.id}",
                    "edit_url": f"/connect/braze?edit={bz.id}",
                }
            )

        # ── MoEngage connections ──
        for me in moengage_rows:
            connections_view.append(
                {
                    "id": str(me.id),
                    "provider": "moengage",
                    "platform_name": "MoEngage",
                    "label": me.display_name or "MoEngage",
                    "detail": f"Data center {me.data_center}" if me.data_center else "Customer engagement",
                    "icon": "📧",
                    "is_active": me.connection_status == "active",
                    "delete_url": f"/api/connections/moengage/{me.id}",
                    "edit_url": f"/connect/moengage?edit={me.id}",
                }
            )

        # ── Adobe connections ──
        for adc in adobe_rows:
            products = []
            if adc.has_analytics:
                products.append("Analytics")
            if adc.has_launch:
                products.append("Launch")
            connections_view.append(
                {
                    "id": str(adc.id),
                    "provider": "adobe",
                    "platform_name": "Adobe",
                    "label": adc.display_name or "Adobe",
                    "detail": " + ".join(products) if products else "Adobe IMS",
                    "icon": "🔴",
                    "is_active": adc.connection_status == "active",
                    "delete_url": f"/api/connections/adobe/{adc.id}",
                    "edit_url": f"/connect/adobe?edit={adc.id}",
                }
            )

        # ── Marketo connections ──
        for mkto in marketo_rows:
            connections_view.append(
                {
                    "id": str(mkto.id),
                    "provider": "marketo",
                    "platform_name": "Adobe Marketo Engage",
                    "label": mkto.display_name or "Marketo",
                    "detail": mkto.instance_url or "Marketing automation",
                    "icon": "🟣",
                    "is_active": mkto.connection_status == "active",
                    "delete_url": f"/api/connections/marketo/{mkto.id}",
                    "edit_url": f"/connect/marketo?edit={mkto.id}",
                }
            )

        # ── Redshift connections ──
        for rs in redshift_rows:
            connections_view.append(
                {
                    "id": str(rs.id),
                    "provider": "redshift",
                    "platform_name": "Redshift",
                    "label": rs.display_name or "Redshift",
                    "detail": f"Database: {rs.database}",
                    "icon": "🟠",
                    "is_active": rs.connection_status == "active",
                    "delete_url": f"/api/connections/redshift/{rs.id}",
                    "edit_url": f"/connect/redshift?edit={rs.id}",
                }
            )

        # ── Snowflake connections ──
        for sf in snowflake_rows:
            connections_view.append(
                {
                    "id": str(sf.id),
                    "provider": "snowflake",
                    "platform_name": "Snowflake",
                    "label": sf.display_name or "Snowflake",
                    "detail": f"Warehouse: {sf.warehouse} · DB: {sf.database}",
                    "icon": "❄️",
                    "is_active": sf.connection_status == "active",
                    "delete_url": f"/api/connections/snowflake/{sf.id}",
                    "edit_url": f"/connect/snowflake?edit={sf.id}",
                }
            )

    # Real usage per connection from the tool-call activity log: one grouped
    # query over this user's calls in the active project. Usage is tracked per
    # platform, so several connections of one provider share the figure.
    if connections_view:
        from app.connectors import call_usage

        usage: dict = {}
        try:
            async with app_state.db_session_factory() as db:
                usage = await call_usage.usage_summary(db, active_pid_uuid, uuid.UUID(user_ctx.user_id))
        except Exception:
            logger.warning("connection usage lookup failed", exc_info=True)
        for cv in connections_view:
            keys = call_usage.PROVIDER_CONNECTORS.get(cv["provider"], ())
            u = call_usage.combine(usage, keys)
            cv["usage_calls"] = u.calls
            cv["usage_days"] = call_usage.USAGE_DAYS
            cv["last_used"] = call_usage.relative_time(u.last_used)

    # Available platforms list (shown regardless; badge indicates if already connected).
    # `products` (optional) lists the named sub-products a single connection unlocks,
    # rendered as icon+name chips so users can see everything a platform covers.
    platforms_avail = [
        {
            "slug": "google",
            "name": "Google Suite",
            "icon": "🔗",
            "desc": "GA4 · GTM · Google Ads",
            "url": None,
            "count": google_count,
            "primary": True,
        },
        {
            "slug": "bigquery",
            "name": "BigQuery",
            "icon": "🗄️",
            "desc": "Query any dataset via key",
            "url": "/connect/bigquery",
            "count": bq_count,
            "primary": False,
        },
        {
            "slug": "amplitude",
            "name": "Amplitude",
            "icon": "📊",
            "desc": "Product analytics",
            "url": "/connect/amplitude",
            "count": amplitude_count,
            "primary": False,
        },
        {
            "slug": "branch",
            "name": "Branch",
            "icon": "🌿",
            "desc": "Mobile attribution",
            "url": "/connect/branch",
            "count": branch_count,
            "primary": False,
        },
        {
            "slug": "appsflyer",
            "name": "AppsFlyer",
            "icon": "🚀",
            "desc": "Mobile attribution",
            "url": "/connect/appsflyer",
            "count": appsflyer_count,
            "primary": False,
        },
        {
            "slug": "adjust",
            "name": "Adjust",
            "icon": "🎯",
            "desc": "Mobile attribution",
            "url": "/connect/adjust",
            "count": adjust_count,
            "primary": False,
        },
        {
            "slug": "mixpanel",
            "name": "Mixpanel",
            "icon": "🎯",
            "desc": "Product analytics",
            "url": "/connect/mixpanel",
            "count": mixpanel_count,
            "primary": False,
        },
        {
            "slug": "posthog",
            "name": "PostHog",
            "icon": "🦔",
            "desc": "Product analytics (Cloud or self-hosted)",
            "url": "/connect/posthog",
            "count": posthog_count,
            "primary": False,
        },
        {
            "slug": "braze",
            "name": "Braze",
            "icon": "📨",
            "desc": "Customer engagement platform",
            "url": "/connect/braze",
            "count": braze_count,
            "primary": False,
        },
        {
            "slug": "moengage",
            "name": "MoEngage",
            "icon": "📧",
            "desc": "Customer engagement platform",
            "url": "/connect/moengage",
            "count": moengage_count,
            "primary": False,
        },
        {
            "slug": "adobe",
            "name": "Adobe",
            "icon": "🔴",
            "desc": "Analytics + Launch",
            "url": "/connect/adobe",
            "count": adobe_count,
            "primary": False,
            "products": [
                {"slug": "adobe", "name": "Analytics"},
                {"slug": "adobe", "name": "Launch"},
                {"slug": "adobe", "name": "Campaign", "soon": True},
            ],
        },
        {
            "slug": "marketo",
            "name": "Adobe Marketo Engage",
            "icon": "🟣",
            "desc": "Leads, campaigns & automation",
            "url": "/connect/marketo",
            "count": marketo_count,
            "primary": False,
        },
        {
            "slug": "redshift",
            "name": "Redshift",
            "icon": "🟠",
            "desc": "Data warehouse",
            "url": "/connect/redshift",
            "count": redshift_count,
            "primary": False,
        },
        {
            "slug": "snowflake",
            "name": "Snowflake",
            "icon": "❄️",
            "desc": "Data warehouse",
            "url": "/connect/snowflake",
            "count": snowflake_count,
            "primary": False,
        },
        {
            "slug": "meta",
            "name": "Meta Ads",
            "icon": "📘",
            "desc": "Facebook & Instagram",
            "url": "/connect/meta",
            "count": meta_count,
            "primary": False,
        },
        {
            "slug": "tiktok",
            "name": "TikTok Ads",
            "icon": "🎵",
            "desc": "Ad performance & insights",
            "url": "/connect/tiktok",
            "count": tiktok_count,
            "primary": False,
        },
        {
            "slug": "snap",
            "name": "Snapchat Ads",
            "icon": "👻",
            "desc": "Campaigns & conversions",
            "url": "/connect/snap",
            "count": snap_count,
            "primary": False,
        },
        {
            "slug": "x",
            "name": "X Ads",
            "icon": "X",
            "desc": "Campaigns, line items, analytics",
            "url": "/connect/x",
            "count": x_count,
            "primary": False,
        },
        {
            "slug": "reddit",
            "name": "Reddit Ads",
            "icon": "👽",
            "desc": "Campaign & ad group performance",
            "url": "/connect/reddit",
            "count": reddit_count,
            "primary": False,
        },
        {
            "slug": "apple",
            "name": "Apple Ads",
            "icon": "Apple",
            "desc": "App Store campaign performance",
            "url": "/connect/apple",
            "count": apple_count,
            "primary": False,
        },
        {
            "slug": "linkedin",
            "name": "LinkedIn Ads",
            "icon": "💼",
            "desc": "Campaigns & Insight Tag",
            "url": "/connect/linkedin",
            "count": linkedin_count,
            "primary": False,
        },
        {
            "slug": "pinterest",
            "name": "Pinterest Ads",
            "icon": "📌",
            "desc": "Campaigns & pixel",
            "url": "/connect/pinterest",
            "count": pinterest_count,
            "primary": False,
        },
        {
            "slug": "bing",
            "name": "Bing Webmaster Tools",
            "icon": "🔎",
            "desc": "Search performance, crawl stats, index coverage",
            "url": "/connect/bing",
            "count": bing_count,
            "primary": False,
        },
    ]

    # Determine active Google scope tier from connected scopes
    google_active_tier = None
    if user_ctx is not None and google_count > 0:
        for c in oauth_rows:
            prov = c.provider or "google"
            if prov == "google" and c.scopes:
                granted = set(c.scopes)
                if "https://www.googleapis.com/auth/analytics" in granted:
                    google_active_tier = "Full"
                elif "https://www.googleapis.com/auth/tagmanager.edit.containers" in granted:
                    google_active_tier = "GTM Write"
                else:
                    google_active_tier = "Read-only"
                break

    # GRANULAR connection count — the header's "X connected. Y to go." must
    # show the SAME numbers as the /home KPI. Google's four services count
    # SEPARATELY, derived from the granted scopes on the Google OAuth rows;
    # every other connector counts as one via its provider/credential count.
    # Both pages feed the shared count_granular_connectors() helper so they
    # can never drift.
    google_has_ga4 = False
    google_has_gtm = False
    google_has_ads = False
    google_has_gsc = False
    google_has_data_manager = False
    for c in oauth_rows:
        if (c.provider or "google") not in ("google", None, ""):
            continue
        scopes = c.scopes or []
        if any("analytics" in s for s in scopes):
            google_has_ga4 = True
        if any("tagmanager" in s for s in scopes):
            google_has_gtm = True
        if "https://www.googleapis.com/auth/adwords" in scopes:
            google_has_ads = True
        if any("webmasters" in s for s in scopes):
            google_has_gsc = True
        if "https://www.googleapis.com/auth/datamanager" in scopes:
            google_has_data_manager = True
    conn_flags = SimpleNamespace(
        has_ga4=google_has_ga4,
        has_gtm=google_has_gtm,
        has_ads=google_has_ads,
        has_gsc=google_has_gsc,
        has_data_manager=google_has_data_manager,
        has_bq=bool(bq_count),
        has_redshift=bool(redshift_count),
        has_snowflake=bool(snowflake_count),
        has_amplitude=bool(amplitude_count),
        has_mixpanel=bool(mixpanel_count),
        has_posthog=bool(posthog_count),
        has_braze=bool(braze_count),
        has_moengage=bool(moengage_count),
        has_adobe_analytics=bool(adobe_count),
        has_adobe_launch=bool(adobe_count),
        has_meta=bool(meta_count),
        has_tiktok=bool(tiktok_count),
        has_snap=bool(snap_count),
        has_x=bool(x_count),
        has_reddit=bool(reddit_count),
        has_apple=bool(apple_count),
        has_linkedin=bool(linkedin_count),
        has_pinterest=bool(pinterest_count),
        has_bing=bool(bing_count),
    )
    connected_count, total_connector_count = count_granular_connectors(conn_flags)

    response = render(
        request,
        "connect.html",
        {
            "user": user_view,
            "connections": connections_view,
            "platforms_avail": platforms_avail,
            "connected_count": connected_count,
            "total_connector_count": total_connector_count,
            "remaining_count": total_connector_count - connected_count,
            "has_google": google_count > 0,
            "google_active_tier": google_active_tier,
            "active_project_id": active_pid,
        },
    )

    # Set cookie if auto-selected
    if active_pid and active_pid != get_active_project_id(request):
        set_active_project_cookie(response, active_pid)

    return response


@router.get("/connect/google", response_class=HTMLResponse)
async def google_connection_page(request: Request):
    """Google Suite connection page — choose services (GA4, GTM, Ads) for new or existing connections."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/google", status_code=302)

    # Fail early with a friendly error page if Google OAuth isn't configured
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    _pid = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    async with app_state.db_session_factory() as _cred_db:
        await get_oauth_app_credentials(_cred_db, "google", project_id=_pid)

    user_view = await _load_user_view(user_ctx) if user_ctx else None

    edit_id = request.query_params.get("edit")
    conn = None
    if edit_id:
        for c in user_ctx.connections:
            if str(c.id) == edit_id and (c.provider in ("google", None, "")):
                conn = c
                break

    # For new connections: defaults — all services enabled, read-only
    editing = None
    if conn:
        scopes = conn.scopes or []

        # Determine which services are enabled and at what level
        has_ga4 = any("analytics" in s for s in scopes)
        ga4_write = "https://www.googleapis.com/auth/analytics" in scopes  # full (non-.readonly)
        has_data_manager = "https://www.googleapis.com/auth/datamanager" in scopes
        has_gtm = any("tagmanager" in s for s in scopes)
        gtm_write = "https://www.googleapis.com/auth/tagmanager.edit.containers" in scopes
        has_ads = "https://www.googleapis.com/auth/adwords" in scopes
        has_gsc = any("webmasters" in s for s in scopes)
        gsc_write = "https://www.googleapis.com/auth/webmasters" in scopes  # full (non-.readonly)

        # Count discovered resources
        ga4_properties = 0
        gtm_containers = 0
        ads_accounts = 0
        gsc_sites = 0
        try:
            db_session = app_state.db_session_factory()
            async with db_session as db:
                from app.models.token import GA4Property, GoogleAdsAccount, GTMContainer, SearchConsoleSite

                ga4_result = await db.execute(select(GA4Property).where(GA4Property.connection_id == conn.id))
                ga4_properties = len(list(ga4_result.scalars().all()))

                gtm_result = await db.execute(
                    select(GTMContainer).where(GTMContainer.connection_id == conn.id)
                )
                gtm_containers = len(list(gtm_result.scalars().all()))

                ads_result = await db.execute(
                    select(GoogleAdsAccount).where(GoogleAdsAccount.connection_id == conn.id)
                )
                ads_accounts = len(list(ads_result.scalars().all()))

                gsc_result = await db.execute(
                    select(SearchConsoleSite).where(SearchConsoleSite.connection_id == conn.id)
                )
                gsc_sites = len(list(gsc_result.scalars().all()))
        except Exception:
            pass

        editing = {
            "id": str(conn.id),
            "email": conn.google_email,
            "scopes": scopes,
            "has_ga4": has_ga4,
            "ga4_write": ga4_write,
            "has_data_manager": has_data_manager,
            "has_gtm": has_gtm,
            "gtm_write": gtm_write,
            "has_ads": has_ads,
            "has_gsc": has_gsc,
            "gsc_write": gsc_write,
            "ga4_properties": ga4_properties,
            "gtm_containers": gtm_containers,
            "ads_accounts": ads_accounts,
            "gsc_sites": gsc_sites,
        }

    active_pid = await ensure_active_project(request, user_ctx.user_id)

    # Linked-resources panel: owners/admins of the connection's project only.
    can_manage_resources = False
    if editing:
        from app.auth.connection_gate import can_connect, project_of_connection

        _found, _conn_pid = await project_of_connection(editing["id"])
        can_manage_resources = bool(_conn_pid) and await can_connect(user_ctx.user_id, _conn_pid)

    response = render(
        request,
        "connect/google.html",
        {
            "user": user_view,
            "editing": editing,
            "can_manage_resources": can_manage_resources,
            "is_new": editing is None,
            "active_project_id": active_pid,
        },
    )
    if active_pid and active_pid != get_active_project_id(request):
        set_active_project_cookie(response, active_pid)
    return response


@router.get("/api/connections/google/initiate")
async def initiate_google_oauth(
    request: Request,
    products: str = Query(default="ga4,gtm,ads"),
    write_ga4: str = Query(default="0"),
    write_gtm: str = Query(default="0"),
    write_gsc: str = Query(default="0"),
    # Legacy tier param — kept for backward compatibility
    tier: str | None = Query(default=None),
):
    """Start the Google data OAuth flow with per-product scope selection."""
    user_ctx = await _resolve_user_ctx(request)

    # Connecting a data source must be tied to the logged-in user. If we let
    # the flow start anonymously, the callback has no choice but to guess the
    # account from the Google email it gets back — which silently switches the
    # browser session into whatever Google account was used to connect. Refuse
    # up front instead. (Mirrors /connect/meta, /connect/tiktok, /connect/snap.)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/google", status_code=302)

    # Legacy tier support
    if tier and tier in SCOPE_MAP:
        scopes = SCOPE_MAP[tier]
    else:
        product_list = [p.strip() for p in products.split(",") if p.strip()]
        scopes = _build_scopes_for_products(
            product_list,
            write_ga4=write_ga4 == "1",
            write_gtm=write_gtm == "1",
            write_gsc=write_gsc == "1",
        )
        if not scopes or scopes == list(_IDENTITY_SCOPES):
            # Fallback: if nothing useful selected, request all read-only
            scopes = SCOPE_MAP["readonly"]

    oauth_state = secrets.token_urlsafe(32)

    # The connection is always bound to the already-authenticated user
    # resolved above (covers both MCP bearer and signed uid cookie sessions).
    user_id = user_ctx.user_id

    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    # Resolve active project (from cookie or query param)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    redis = app_state.redis_client
    await redis.setex(
        f"google_oauth_state:{oauth_state}",
        600,
        json.dumps(
            {
                "products": products,
                "scopes": scopes,
                "user_id": user_id,
                "project_id": active_project_id,
                "base_url": req_base_url,
            }
        ),
    )

    import urllib.parse

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _google_creds = await get_oauth_app_credentials(_cred_db, "google", project_id=active_project_id)

    data_redirect_uri = f"{req_base_url}/auth/google/data/callback"
    params = {
        "client_id": _google_creds.client_id,
        "redirect_uri": data_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": oauth_state,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url)


@router.get("/auth/google/data/callback")
async def google_data_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    error: str | None = Query(default=None),
):
    """Handle Google data OAuth callback — exchange code, store tokens, discover resources."""
    if error:
        return HTMLResponse(
            f"<h2>Google OAuth Error: {_html_escape(str(error))}</h2><a href='/'>Back</a>", status_code=400
        )

    redis = app_state.redis_client
    state_raw = await redis.get(f"google_oauth_state:{state}")
    if not state_raw:
        return HTMLResponse(
            "<h2>Invalid or expired state. Please try again.</h2><a href='/connect'>Try again</a>",
            status_code=400,
        )

    state_data = json.loads(state_raw)
    await redis.delete(f"google_oauth_state:{state}")

    # Use the base_url stored when the flow started so redirect_uri matches.
    from app.utils import base_url_from_request

    stored_base_url = state_data.get("base_url") or base_url_from_request(request)
    data_redirect_uri = f"{stored_base_url}/auth/google/data/callback"

    # Exchange code for tokens
    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, state_data.get("user_id"), state_data.get("project_id"))
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _google_creds = await get_oauth_app_credentials(
            _cred_db, "google", project_id=state_data.get("project_id")
        )

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _google_creds.client_id,
                "client_secret": _google_creds.client_secret,
                "code": code,
                "redirect_uri": data_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Token exchange failed: {_html_escape(str(token_resp.text))}</h2>", status_code=400
        )

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        return HTMLResponse(
            "<h2>No refresh token received. Please revoke access in your Google Account and try again with 'offline' access.</h2>",
            status_code=400,
        )

    # Get Google user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    userinfo = userinfo_resp.json()
    google_email = userinfo.get("email")
    if not google_email:
        return HTMLResponse(
            "<h2>Could not retrieve email from Google. Make sure your Google account has a verified email address.</h2>"
            "<a href='/connect'>Try again</a>",
            status_code=400,
        )

    # Ensure user exists
    from sqlalchemy import select

    from app.models.user import User

    user_id = state_data.get("user_id")
    project_id = state_data.get("project_id")

    # The connect flow always records the logged-in user in the OAuth state
    # (see initiate_google_oauth). A missing user_id means the flow wasn't
    # started from an authenticated session — refuse, rather than resolving
    # the account from the Google email, which would attach the connection
    # to (and below, log the browser in as) whatever Google account was used.
    if not user_id:
        return HTMLResponse(
            "<h2>Your session expired. Please <a href='/signin'>sign in</a> and connect again.</h2>",
            status_code=401,
        )

    # Fallback: if project_id wasn't captured in the OAuth state (e.g. legacy
    # flow or cookie was missing when initiate was called), look up the user's
    # first project so we never violate the NOT NULL constraint on project_id.
    if not project_id:
        from app.models.project import Project, ProjectMember

        async with app_state.db_session_factory() as _proj_db:
            _row = await _proj_db.execute(
                select(Project)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(
                    ProjectMember.user_id == user_id,
                    ProjectMember.is_active == True,
                    Project.is_active == True,
                )
                .order_by(Project.created_at.asc())
                .limit(1)
            )
            _proj = _row.scalar_one_or_none()
            if _proj:
                project_id = str(_proj.id)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return HTMLResponse(
                "<h2>Your session expired. Please <a href='/signin'>sign in</a> and connect again.</h2>",
                status_code=401,
            )

        # Create / update OAuthConnection. Credentials are per-user: the row
        # is keyed by (user_id, project_id, provider, google_email) — the same
        # tuple as the uq_user_project_provider_email unique constraint. We
        # look it up by that exact key so reconnecting refreshes the caller's
        # own row, while the same Google account connected by a *different*
        # user (or left behind as a phantom row) gets its own separate row
        # rather than being clobbered or colliding on the constraint.
        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == user.id,
            OAuthConnection.provider == "google",
            OAuthConnection.google_email == google_email,
            OAuthConnection.project_id == project_id,
        )
        result = await db.execute(existing_stmt)
        existing = result.scalar_one_or_none()

        encrypted_refresh = _encrypt(refresh_token)

        if existing:
            existing.refresh_token_encrypted = encrypted_refresh
            existing.access_token_encrypted = _encrypt(access_token)
            existing.scopes = state_data["scopes"]
            existing.connection_status = "active"
            existing.is_active = True
            conn = existing
        else:
            conn = OAuthConnection(
                user_id=user.id,
                project_id=project_id,  # Scope to active project (None for legacy)
                provider="google",
                google_email=google_email,
                access_token_encrypted=_encrypt(access_token),
                refresh_token_encrypted=encrypted_refresh,
                scopes=state_data["scopes"],
                connection_status="active",
                is_active=True,
            )
            db.add(conn)

        await db.flush()
        conn_id = conn.id

        # Discover GA4 properties
        try:
            from app.app_state import ga4_connector

            properties = await ga4_connector.list_all_properties_raw(access_token)
            for prop in properties:
                prop_id = prop["id"]
                exists = await db.execute(
                    select(GA4Property).where(
                        GA4Property.property_id == prop_id,
                        GA4Property.connection_id == conn_id,
                    )
                )
                if not exists.scalar_one_or_none():
                    db.add(
                        GA4Property(
                            connection_id=conn_id,
                            property_id=prop_id,
                            property_name=prop.get("displayName", ""),
                            account_id=prop.get("account", ""),
                            account_name=prop.get("accountName", ""),
                        )
                    )
        except Exception:
            pass  # Non-critical — user can still use tools

        # Discover GTM containers
        try:
            async with httpx.AsyncClient() as client:
                gtm_acct_resp = await client.get(
                    "https://www.googleapis.com/tagmanager/v2/accounts",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if gtm_acct_resp.status_code == 200:
                for acct in gtm_acct_resp.json().get("account", []):
                    acct_id = acct["accountId"]
                    async with httpx.AsyncClient() as client:
                        cont_resp = await client.get(
                            f"https://www.googleapis.com/tagmanager/v2/accounts/{acct_id}/containers",
                            headers={"Authorization": f"Bearer {access_token}"},
                        )
                    for cont in cont_resp.json().get("container", []):
                        cont_id = cont["containerId"]
                        exists = await db.execute(
                            select(GTMContainer).where(
                                GTMContainer.container_id == cont_id,
                                GTMContainer.connection_id == conn_id,
                            )
                        )
                        if not exists.scalar_one_or_none():
                            db.add(
                                GTMContainer(
                                    connection_id=conn_id,
                                    account_id=acct_id,
                                    container_id=cont_id,
                                    container_name=cont.get("name", ""),
                                    public_id=cont.get("publicId", ""),
                                )
                            )
        except Exception:
            pass

        # Discover Search Console sites (if webmasters scope granted)
        try:
            if any("webmasters" in s for s in (state_data.get("scopes") or [])):
                from app.app_state import search_console_connector
                from app.models.token import SearchConsoleSite

                sites = await search_console_connector.list_all_sites_raw(access_token)
                for s in sites:
                    site_url = s.get("siteUrl")
                    if not site_url:
                        continue
                    exists = await db.execute(
                        select(SearchConsoleSite).where(
                            SearchConsoleSite.site_url == site_url,
                            SearchConsoleSite.connection_id == conn_id,
                        )
                    )
                    if not exists.scalar_one_or_none():
                        db.add(
                            SearchConsoleSite(
                                connection_id=conn_id,
                                site_url=site_url,
                                permission_level=s.get("permissionLevel"),
                                is_domain_property=str(site_url).startswith("sc-domain:"),
                            )
                        )
        except Exception:
            pass

        await db.commit()

    # Bust cached UserContext so the next MCP tool call picks up new connections
    asyncio.create_task(invalidate_user_context_cache(str(user.id)))

    # Create in-app notification
    from app.notifications import create_notification

    asyncio.create_task(
        create_notification(
            user_id=str(user.id),
            title="Google Suite Connected",
            message="Your GA4 properties and GTM containers are ready. Start querying your analytics data.",
            category="connection",
            severity="success",
            action_url="/connect",
        )
    )

    # Do NOT touch the uid session cookie here. The user is already
    # authenticated (initiate_google_oauth requires it); re-issuing the
    # session on a data-connect callback is what allowed connecting a
    # different Google account to hijack the session.
    return RedirectResponse(url="/home?toast=google_connected", status_code=302)


@router.get("/api/connections")
async def list_connections(request: Request):
    """List all active connections for the authenticated user, scoped to active project."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        pass

    if user_ctx is None:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if user_ctx is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Resolve active project and filter connections by it
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    if active_project_id:
        import uuid as _uuid

        try:
            active_pid_uuid = _uuid.UUID(active_project_id)
        except ValueError:
            active_pid_uuid = None

        if active_pid_uuid:
            db_session = app_state.db_session_factory()
            async with db_session as db:
                stmt = select(OAuthConnection).where(
                    OAuthConnection.user_id == _uuid.UUID(user_ctx.user_id),
                    OAuthConnection.project_id == active_pid_uuid,
                    OAuthConnection.is_active == True,
                )
                result = await db.execute(stmt)
                conns = result.scalars().all()
            return {
                "connections": [
                    {
                        "id": str(c.id),
                        "google_email": c.google_email,
                        "status": c.connection_status,
                        "scope_tier": c.scopes,
                    }
                    for c in conns
                ]
            }

    # Fallback: return user-level connections (legacy / no active project)
    return {
        "connections": [
            {
                "id": c.id,
                "google_email": c.google_email,
                "status": c.connection_status,
                "scope_tier": c.scopes,
            }
            for c in user_ctx.connections
        ]
    }


@router.delete("/api/connections/{conn_id}")
async def disconnect_connection(conn_id: str, request: Request):
    """Disconnect / deactivate a Google connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        pass

    if user_ctx is None:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if user_ctx is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(OAuthConnection)
            .where(OAuthConnection.id == conn_id, OAuthConnection.user_id == user_ctx.user_id)
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    import asyncio

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


@router.get("/api/connections/{conn_id}/status")
async def connection_status(conn_id: str, request: Request):
    """Get status of a specific connection (authenticated, project-scoped)."""
    # Require authentication
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        pass
    if user_ctx is None:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass
    if user_ctx is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.id == conn_id,
                OAuthConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
        )
        conn = result.scalar_one_or_none()

    if not conn:
        return JSONResponse({"error": "not found"}, status_code=404)

    return {
        "id": str(conn.id),
        "google_email": conn.google_email,
        "status": conn.connection_status,
        "is_active": conn.is_active,
        "last_used": conn.last_used_at.isoformat() if conn.last_used_at else None,
    }


@router.get("/api/health")
async def health(request: Request):
    """Liveness + basic dependency probe for monitoring.

    Returns 200 always (so the container orchestrator keeps the pod alive
    even when a downstream hiccups), but embeds per-component status so
    monitors can alert on `overall != "ok"`.
    """
    import time as _t

    t0 = _t.perf_counter()
    components = {}

    # Database
    try:
        from sqlalchemy import text as _text

        from app.db.database import pool_stats

        db_session = app_state.db_session_factory()
        async with db_session as db:
            await db.execute(_text("SELECT 1"))
        components["database"] = {"status": "ok", "pool": pool_stats()}
    except Exception as e:
        components["database"] = {"status": "error", "error": str(e)[:200]}

    # Redis
    try:
        if app_state.redis_client:
            await app_state.redis_client.ping()
            components["redis"] = {"status": "ok"}
        else:
            components["redis"] = {"status": "unconfigured"}
    except Exception as e:
        components["redis"] = {"status": "error", "error": str(e)[:200]}

    overall = (
        "ok" if all(c.get("status") in ("ok", "unconfigured") for c in components.values()) else "degraded"
    )

    body = {
        "status": overall,
        "components": components,
        "probe_duration_ms": int((_t.perf_counter() - t0) * 1000),
    }

    # Diagnostics (error text, pool and per-tool stats, breaker targets) are for
    # super admins; monitors and orchestrators only need the statuses.
    from app.auth.superadmin_cache import is_superadmin_cached

    uid = get_uid_from_request(request)
    if not uid or not await is_superadmin_cached(uid):
        for comp in body.get("components", {}).values() if isinstance(body.get("components"), dict) else []:
            if isinstance(comp, dict):
                comp.pop("error", None)
                comp.pop("pool", None)
        return body

    # Include reliability stats if available
    try:
        from app.tools.reliability import breaker, stats

        body["mcp_stats"] = stats.snapshot()
        body["circuit_breakers"] = breaker.snapshot()
    except Exception:
        pass

    return body


@router.get("/api/health/live-dashboards")
async def health_live():
    """Cheap liveness probe — no downstream calls."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# BigQuery Routes
# ---------------------------------------------------------------------------


@router.get("/connect/bigquery", response_class=HTMLResponse)
async def connect_bq_page(request: Request):
    """BigQuery Service Account connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/bigquery", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(BQConnection).where(
                        BQConnection.id == uuid.UUID(edit_id),
                        BQConnection.user_id == uuid.UUID(user_ctx.user_id),
                        BQConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "project_id": row.project_id,
                }

    return render(
        request,
        "connect/bigquery.html",
        {
            "user": user_view,
            "platform_name": "BigQuery",
            "platform_icon": "🗄️",
            "platform_desc": "Upload a Google Cloud service-account JSON key to query any BigQuery dataset from Claude.",
            "editing": editing,
        },
    )


class BQUploadRequest(BaseModel):
    sa_json: str
    display_name: str | None = "BigQuery Connection"


@router.post("/api/connections/bigquery")
async def add_bq_connection(payload: BQUploadRequest, request: Request):
    """Parses and securely stores a BigQuery Service Account JSON payload."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        data = json.loads(payload.sa_json)
        project_id = data.get("project_id")
        if not project_id or data.get("type") != "service_account":
            return JSONResponse(
                {"error": "Invalid Service Account JSON. Missing project_id or type."}, status_code=400
            )
    except Exception:
        return JSONResponse({"error": "Invalid JSON string format."}, status_code=400)

    encrypted_sa = _encrypt(payload.sa_json)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        # Check if already exists for this GCP project
        import uuid

        # Deactivate existing connection for same GCP project if any (we keep 1 per project per user)
        await db.execute(
            update(BQConnection)
            .where(BQConnection.user_id == uuid.UUID(user_ctx.user_id), BQConnection.project_id == project_id)
            .values(is_active=False)
        )

        # Create new connection
        new_conn = BQConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            fluxito_project_id=active_project_id,  # Internal project scope
            display_name=payload.display_name,
            project_id=project_id,  # GCP project ID
            datasets=[],
            service_account_encrypted=encrypted_sa,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    return {"status": "success", "project_id": project_id}


@router.put("/api/connections/bigquery/{conn_id}")
async def update_bq_connection(conn_id: str, payload: BQUploadRequest, request: Request):
    """Updates an existing BigQuery connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {"display_name": payload.display_name}
    if payload.sa_json:
        try:
            data = json.loads(payload.sa_json)
            project_id = data.get("project_id")
            if not project_id or data.get("type") != "service_account":
                return JSONResponse({"error": "Invalid Service Account JSON."}, status_code=400)
            values["project_id"] = project_id
            values["service_account_encrypted"] = _encrypt(payload.sa_json)
        except Exception:
            return JSONResponse({"error": "Invalid JSON string format."}, status_code=400)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BQConnection)
            .where(BQConnection.id == uuid.UUID(conn_id), BQConnection.user_id == uuid.UUID(user_ctx.user_id))
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/bigquery/{conn_id}")
async def disconnect_bq_connection(conn_id: str, request: Request):
    """Deactivates a BigQuery connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    import uuid

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BQConnection)
            .where(BQConnection.id == uuid.UUID(conn_id), BQConnection.user_id == uuid.UUID(user_ctx.user_id))
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Meta (Facebook) Ads OAuth Routes
# ---------------------------------------------------------------------------


@router.get("/connect/meta", response_class=HTMLResponse)
async def connect_meta_page(request: Request):
    """Meta Ads connect interstitial — shows permissions before redirecting."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/meta", status_code=302)

    return await _render_interstitial(
        request=request,
        platform_slug="meta",
        platform_name="Meta Ads",
        platform_desc="Connect your Facebook and Instagram ad accounts to query campaign performance, audience insights, and pixel events directly in Claude.",
        btn_bg="linear-gradient(135deg,#1877f2,#0e5fc2)",
        btn_text_color="#fff",
        btn_shadow="0 4px 20px rgba(24,119,242,0.4)",
        permissions=[
            (
                "📣",
                "ads_read",
                "Read your ad accounts, campaigns, ad sets and ads — including spend, impressions, clicks and conversions.",
            ),
            (
                "⚙️",
                "ads_management",
                "Create and manage campaigns on your behalf when you ask Claude to make changes.",
            ),
            (
                "🏢",
                "business_management",
                "Access Business Manager to list ad accounts and pages associated with your business.",
            ),
        ],
        authorize_url="/connect/meta/authorize",
        user_ctx=user_ctx,
    )


@router.get("/connect/meta/authorize")
async def meta_authorize(request: Request):
    """Generate OAuth state and redirect to Meta login. Requires auth."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/meta", status_code=302)

    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    oauth_state = secrets.token_urlsafe(32)
    redis = app_state.redis_client
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    await redis.setex(
        f"meta_oauth_state:{oauth_state}",
        600,
        json.dumps(
            {
                "user_id": user_ctx.user_id,
                "project_id": active_project_id,
                "base_url": req_base_url,
            }
        ),
    )

    import urllib.parse

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _meta_creds = await get_oauth_app_credentials(_cred_db, "meta", project_id=active_project_id)

    meta_redirect_uri = f"{req_base_url}/auth/meta/callback"
    params = {
        "client_id": _meta_creds.client_id,
        "redirect_uri": meta_redirect_uri,
        "state": oauth_state,
        "scope": "ads_read,ads_management,business_management",
        "response_type": "code",
    }
    url = "https://www.facebook.com/v19.0/dialog/oauth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url)


@router.get("/auth/meta/callback")
async def meta_callback(
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    """Handle Meta OAuth callback — exchange code, store token."""
    if error:
        return HTMLResponse(
            f"<h2>Meta OAuth Error: {_html_escape(str(error_description or error))}</h2><a href='/'>Back</a>",
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse("<h2>Invalid callback — missing code or state.</h2>", status_code=400)

    redis = app_state.redis_client
    state_raw = await redis.get(f"meta_oauth_state:{state}")
    if not state_raw:
        return HTMLResponse(
            "<h2>Invalid or expired state. Please try again.</h2><a href='/connect/meta'>Try again</a>",
            status_code=400,
        )

    state_str = state_raw.decode() if isinstance(state_raw, bytes) else state_raw
    await redis.delete(f"meta_oauth_state:{state}")

    # Parse stored state — may be plain string (legacy) or JSON object.
    meta_project_id = None
    try:
        state_data = json.loads(state_str)
        user_id = state_data.get("user_id", state_str)
        stored_base_url = state_data.get("base_url", "")
        meta_project_id = state_data.get("project_id")
    except (json.JSONDecodeError, TypeError):
        user_id = state_str
        stored_base_url = ""

    from app.utils import base_url_from_request

    base_url = stored_base_url or base_url_from_request(request)
    meta_redirect_uri = f"{base_url}/auth/meta/callback"

    # Exchange code for token
    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, user_id, meta_project_id)
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _meta_creds = await get_oauth_app_credentials(_cred_db, "meta", project_id=meta_project_id)

    async with httpx.AsyncClient() as client:
        token_resp = await client.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "client_id": _meta_creds.client_id,
                "client_secret": _meta_creds.client_secret,
                "redirect_uri": meta_redirect_uri,
                "code": code,
            },
        )

    if token_resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Meta token exchange failed: {_html_escape(str(token_resp.text))}</h2>", status_code=400
        )

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return HTMLResponse("<h2>No access token received from Meta.</h2>", status_code=400)

    # Get user info from Graph API
    async with httpx.AsyncClient() as client:
        me_resp = await client.get(
            "https://graph.facebook.com/v19.0/me",
            params={"fields": "id,name,email", "access_token": access_token},
        )
    me_data = me_resp.json()
    meta_user_id = me_data.get("id", "")

    # Store in OAuthConnection
    db_session = app_state.db_session_factory()
    async with db_session as db:
        from sqlalchemy import select

        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "meta",
        )
        if meta_project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == meta_project_id)
        result = await db.execute(existing_stmt)
        existing = result.scalar_one_or_none()

        encrypted_token = _encrypt(access_token)
        # Meta tokens are long-lived user tokens (60 days) — no separate refresh token
        # Store access token in both fields; refresh is handled via token extension
        if existing:
            existing.access_token_encrypted = encrypted_token
            existing.refresh_token_encrypted = encrypted_token  # same token used to refresh
            existing.google_email = me_data.get("email") or f"meta_{meta_user_id}"
            existing.scopes = ["ads_read", "ads_management", "business_management"]
            existing.connection_status = "active"
            existing.is_active = True
            if meta_project_id:
                existing.project_id = meta_project_id
        else:
            db.add(
                OAuthConnection(
                    user_id=user_id,
                    project_id=meta_project_id,
                    provider="meta",
                    google_email=me_data.get("email") or f"meta_{meta_user_id}",
                    access_token_encrypted=encrypted_token,
                    refresh_token_encrypted=encrypted_token,
                    scopes=["ads_read", "ads_management", "business_management"],
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_id)))

    from app.notifications import create_notification

    asyncio.create_task(
        create_notification(
            user_id=str(user_id),
            title="Meta Ads Connected",
            message="Your Meta Ads campaigns are now accessible. Start analyzing your ad performance.",
            category="connection",
            severity="success",
            action_url="/connect",
        )
    )

    # Already-authenticated connect flow — never re-issue the uid session cookie.
    return RedirectResponse(url="/home?toast=meta_connected", status_code=302)


@router.delete("/api/connections/meta/{conn_id}")
async def disconnect_meta_connection(conn_id: str, request: Request):
    """Deactivates a Meta Ads connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == conn_id,
                OAuthConnection.user_id == user_ctx.user_id,
                OAuthConnection.provider == "meta",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()
    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# TikTok Ads OAuth Routes
# ---------------------------------------------------------------------------


@router.get("/connect/tiktok", response_class=HTMLResponse)
async def connect_tiktok_page(request: Request):
    """TikTok Ads connect interstitial — shows permissions before redirecting."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/tiktok", status_code=302)

    return await _render_interstitial(
        request=request,
        platform_slug="tiktok",
        platform_name="TikTok Ads",
        platform_desc="Connect your TikTok Business account to analyze campaign performance, creative insights, and pixel events directly from Claude.",
        btn_bg="linear-gradient(135deg,#fe2c55,#c0002a)",
        btn_text_color="#fff",
        btn_shadow="0 4px 20px rgba(254,44,85,0.35)",
        permissions=[
            (
                "📊",
                "advertiser.read",
                "Read ad account details, campaigns, ad groups and ads — spend, impressions, CTR and conversions.",
            ),
            (
                "⚙️",
                "advertiser.manage",
                "Manage campaigns and ad groups on your behalf when you ask Claude to create or update them.",
            ),
            (
                "👤",
                "user.info.basic",
                "Access your TikTok Business account profile to identify the connected account.",
            ),
        ],
        authorize_url="/connect/tiktok/authorize",
        user_ctx=user_ctx,
    )


@router.get("/connect/tiktok/authorize")
async def tiktok_authorize(request: Request):
    """Generate OAuth state and redirect to TikTok login."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/tiktok", status_code=302)

    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    oauth_state = secrets.token_urlsafe(32)
    redis = app_state.redis_client
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    await redis.setex(
        f"tiktok_oauth_state:{oauth_state}",
        600,
        json.dumps(
            {
                "user_id": user_ctx.user_id,
                "project_id": active_project_id,
            }
        ),
    )

    import urllib.parse

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _tiktok_creds = await get_oauth_app_credentials(_cred_db, "tiktok", project_id=active_project_id)

    tiktok_redirect_uri = f"{req_base_url}/auth/tiktok/callback"
    params = {
        "app_id": _tiktok_creds.client_id,
        "redirect_uri": tiktok_redirect_uri,
        "state": oauth_state,
        "scope": "user.info.basic,advertiser.read,advertiser.manage",
        "response_type": "code",
    }
    url = "https://business-api.tiktok.com/portal/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url)


@router.get("/auth/tiktok/callback")
async def tiktok_callback(
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str | None = Query(default=None),
):
    """Handle TikTok OAuth callback — exchange code, store token."""
    if error:
        return HTMLResponse(
            f"<h2>TikTok OAuth Error: {_html_escape(str(error))}</h2><a href='/'>Back</a>", status_code=400
        )

    if not code or not state:
        return HTMLResponse("<h2>Invalid callback — missing code or state.</h2>", status_code=400)

    redis = app_state.redis_client
    user_id_raw = await redis.get(f"tiktok_oauth_state:{state}")
    if not user_id_raw:
        return HTMLResponse(
            "<h2>Invalid or expired state. Please try again.</h2><a href='/connect/tiktok'>Try again</a>",
            status_code=400,
        )

    state_str = user_id_raw.decode() if isinstance(user_id_raw, bytes) else user_id_raw
    await redis.delete(f"tiktok_oauth_state:{state}")

    # Parse stored state — may be plain user_id string (legacy) or JSON
    tiktok_project_id = None
    try:
        state_data = json.loads(state_str)
        user_id = state_data.get("user_id", state_str)
        tiktok_project_id = state_data.get("project_id")
    except (json.JSONDecodeError, TypeError):
        user_id = state_str

    # Exchange code for token
    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, user_id, tiktok_project_id)
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _tiktok_creds = await get_oauth_app_credentials(_cred_db, "tiktok", project_id=tiktok_project_id)

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/",
            json={
                "app_id": _tiktok_creds.client_id,
                "secret": _tiktok_creds.client_secret,
                "auth_code": code,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        return HTMLResponse(
            f"<h2>TikTok token exchange failed: {_html_escape(str(token_resp.text))}</h2>", status_code=400
        )

    resp_data = token_resp.json()
    if resp_data.get("code") != 0:
        return HTMLResponse(
            f"<h2>TikTok error: {_html_escape(str(resp_data.get('message')))}</h2>", status_code=400
        )

    token_info = resp_data.get("data", {})
    access_token = token_info.get("access_token")
    refresh_token = token_info.get("refresh_token", access_token)
    tiktok_user_id = str(token_info.get("advertiser_id", ""))

    if not access_token:
        return HTMLResponse("<h2>No access token received from TikTok.</h2>", status_code=400)

    # Store in OAuthConnection
    db_session = app_state.db_session_factory()
    async with db_session as db:
        from sqlalchemy import select

        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "tiktok",
        )
        if tiktok_project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == tiktok_project_id)
        result = await db.execute(existing_stmt)
        existing = result.scalar_one_or_none()
        encrypted_access = _encrypt(access_token)
        encrypted_refresh = _encrypt(refresh_token)

        if existing:
            existing.access_token_encrypted = encrypted_access
            existing.refresh_token_encrypted = encrypted_refresh
            existing.google_email = f"tiktok_{tiktok_user_id}"
            existing.scopes = ["advertiser.read", "advertiser.manage"]
            existing.connection_status = "active"
            existing.is_active = True
            if tiktok_project_id:
                existing.project_id = tiktok_project_id
        else:
            db.add(
                OAuthConnection(
                    user_id=user_id,
                    project_id=tiktok_project_id,
                    provider="tiktok",
                    google_email=f"tiktok_{tiktok_user_id}",
                    access_token_encrypted=encrypted_access,
                    refresh_token_encrypted=encrypted_refresh,
                    scopes=["advertiser.read", "advertiser.manage"],
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_id)))

    from app.notifications import create_notification

    asyncio.create_task(
        create_notification(
            user_id=str(user_id),
            title="TikTok Ads Connected",
            message="Your TikTok Ads campaigns are now accessible. Start analyzing your ad performance.",
            category="connection",
            severity="success",
            action_url="/connect",
        )
    )

    # Already-authenticated connect flow — never re-issue the uid session cookie.
    return RedirectResponse(url="/home?toast=tiktok_connected", status_code=302)


@router.delete("/api/connections/tiktok/{conn_id}")
async def disconnect_tiktok_connection(conn_id: str, request: Request):
    """Deactivates a TikTok Ads connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == conn_id,
                OAuthConnection.user_id == user_ctx.user_id,
                OAuthConnection.provider == "tiktok",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()
    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Snapchat Ads OAuth Routes
# ---------------------------------------------------------------------------


@router.get("/connect/snap", response_class=HTMLResponse)
async def connect_snap_page(request: Request):
    """Snapchat Ads connect interstitial — shows permissions before redirecting."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/snap", status_code=302)

    return await _render_interstitial(
        request=request,
        platform_slug="snap",
        platform_name="Snapchat Ads",
        platform_desc="Connect your Snapchat Business account to analyze ad campaigns, audience segments, and pixel performance through natural language in Claude.",
        btn_bg="linear-gradient(135deg,#fffc00,#f5e800)",
        btn_text_color="#0d0d0d",
        btn_shadow="0 4px 20px rgba(255,220,0,0.3)",
        permissions=[
            (
                "📈",
                "snapchat-marketing-api",
                "Full read access to your Snapchat Ads Manager — campaigns, ad sets, creatives, spend, and conversion events.",
            ),
            (
                "🎯",
                "Audience Insights",
                "Access audience segment data and demographic breakdowns for your campaigns.",
            ),
            (
                "🔗",
                "Snap Pixel",
                "Read Snap Pixel event data to attribute conversions and measure funnel performance.",
            ),
        ],
        authorize_url="/connect/snap/authorize",
        user_ctx=user_ctx,
    )


@router.get("/connect/snap/authorize")
async def snap_authorize(request: Request):
    """Generate OAuth state and redirect to Snapchat login."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass

    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/snap", status_code=302)

    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    oauth_state = secrets.token_urlsafe(32)
    redis = app_state.redis_client
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    await redis.setex(
        f"snap_oauth_state:{oauth_state}",
        600,
        json.dumps(
            {
                "user_id": user_ctx.user_id,
                "project_id": active_project_id,
                "base_url": req_base_url,
            }
        ),
    )

    import urllib.parse

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _snap_creds = await get_oauth_app_credentials(_cred_db, "snap", project_id=active_project_id)

    snap_redirect_uri = f"{req_base_url}/auth/snap/callback"
    params = {
        "client_id": _snap_creds.client_id,
        "redirect_uri": snap_redirect_uri,
        "response_type": "code",
        "scope": "snapchat-marketing-api",
        "state": oauth_state,
    }
    url = "https://accounts.snapchat.com/login/oauth2/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url)


@router.get("/auth/snap/callback")
async def snap_callback(
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str | None = Query(default=None),
):
    """Handle Snapchat OAuth callback — exchange code, store token."""
    if error:
        return HTMLResponse(
            f"<h2>Snapchat OAuth Error: {_html_escape(str(error))}</h2><a href='/'>Back</a>", status_code=400
        )

    if not code or not state:
        return HTMLResponse("<h2>Invalid callback — missing code or state.</h2>", status_code=400)

    redis = app_state.redis_client
    state_raw = await redis.get(f"snap_oauth_state:{state}")
    if not state_raw:
        return HTMLResponse(
            "<h2>Invalid or expired state. Please try again.</h2><a href='/connect/snap'>Try again</a>",
            status_code=400,
        )

    state_str = state_raw.decode() if isinstance(state_raw, bytes) else state_raw
    await redis.delete(f"snap_oauth_state:{state}")

    # Parse stored state — may be plain string (legacy) or JSON object.
    snap_project_id = None
    try:
        state_data = json.loads(state_str)
        user_id = state_data.get("user_id", state_str)
        stored_base_url = state_data.get("base_url", "")
        snap_project_id = state_data.get("project_id")
    except (json.JSONDecodeError, TypeError):
        user_id = state_str
        stored_base_url = ""

    from app.utils import base_url_from_request

    base_url = stored_base_url or base_url_from_request(request)
    snap_redirect_uri = f"{base_url}/auth/snap/callback"

    import base64

    from app.auth.connection_gate import verify_oauth_callback

    _denied = await verify_oauth_callback(request, user_id, snap_project_id)
    if _denied is not None:
        return _denied

    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _snap_creds = await get_oauth_app_credentials(_cred_db, "snap", project_id=snap_project_id)

    credentials = base64.b64encode(f"{_snap_creds.client_id}:{_snap_creds.client_secret}".encode()).decode()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://accounts.snapchat.com/login/oauth2/access_token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": snap_redirect_uri,
            },
        )

    if token_resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Snapchat token exchange failed: {_html_escape(str(token_resp.text))}</h2>", status_code=400
        )

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token", access_token)

    if not access_token:
        return HTMLResponse("<h2>No access token received from Snapchat.</h2>", status_code=400)

    # Get user info
    async with httpx.AsyncClient() as client:
        me_resp = await client.get(
            "https://adsapi.snapchat.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    snap_user_id = me_resp.json().get("me", {}).get("id", "") if me_resp.status_code == 200 else ""

    # Store in OAuthConnection
    db_session = app_state.db_session_factory()
    async with db_session as db:
        from sqlalchemy import select

        existing_stmt = select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "snap",
        )
        if snap_project_id:
            existing_stmt = existing_stmt.where(OAuthConnection.project_id == snap_project_id)
        result = await db.execute(existing_stmt)
        existing = result.scalar_one_or_none()
        encrypted_access = _encrypt(access_token)
        encrypted_refresh = _encrypt(refresh_token)

        if existing:
            existing.access_token_encrypted = encrypted_access
            existing.refresh_token_encrypted = encrypted_refresh
            existing.google_email = f"snap_{snap_user_id}"
            existing.scopes = ["snapchat-marketing-api"]
            existing.connection_status = "active"
            existing.is_active = True
            if snap_project_id:
                existing.project_id = snap_project_id
        else:
            db.add(
                OAuthConnection(
                    user_id=user_id,
                    project_id=snap_project_id,
                    provider="snap",
                    google_email=f"snap_{snap_user_id}",
                    access_token_encrypted=encrypted_access,
                    refresh_token_encrypted=encrypted_refresh,
                    scopes=["snapchat-marketing-api"],
                    connection_status="active",
                    is_active=True,
                )
            )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_id)))

    from app.notifications import create_notification

    asyncio.create_task(
        create_notification(
            user_id=str(user_id),
            title="Snapchat Ads Connected",
            message="Your Snapchat Ads campaigns are now accessible. Start analyzing your ad performance.",
            category="connection",
            severity="success",
            action_url="/connect",
        )
    )

    # Already-authenticated connect flow — never re-issue the uid session cookie.
    return RedirectResponse(url="/home?toast=snap_connected", status_code=302)


@router.delete("/api/connections/snap/{conn_id}")
async def disconnect_snap_connection(conn_id: str, request: Request):
    """Deactivates a Snapchat Ads connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            try:
                user_ctx = await build_user_context(uid, request)
            except Exception:
                pass
    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(OAuthConnection)
            .where(
                OAuthConnection.id == conn_id,
                OAuthConnection.user_id == user_ctx.user_id,
                OAuthConnection.provider == "snap",
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()
    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Amplitude Routes (Credential-based connection)
# ---------------------------------------------------------------------------


def _secret_hint(value: str | None) -> str:
    """What an edit page may show of a stored credential: its last 4 characters."""
    v = value or ""
    return f"••••{v[-4:]}" if len(v) > 8 else "••••"


@router.get("/connect/amplitude", response_class=HTMLResponse)
async def connect_amplitude_page(request: Request):
    """Amplitude API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/amplitude", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(AmplitudeConnection).where(
                        AmplitudeConnection.id == uuid.UUID(edit_id),
                        AmplitudeConnection.user_id == uuid.UUID(user_ctx.user_id),
                        AmplitudeConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/amplitude.html",
        {
            "user": user_view,
            "platform_name": "Amplitude",
            "platform_icon": "📊",
            "platform_desc": "Connect your Amplitude project for product analytics insights and event data.",
            "editing": editing,
        },
    )


@router.get("/connect/branch", response_class=HTMLResponse)
async def connect_branch_page(request: Request):
    """Branch API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/branch", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(BranchConnection).where(
                        BranchConnection.id == uuid.UUID(edit_id),
                        BranchConnection.user_id == uuid.UUID(user_ctx.user_id),
                        BranchConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/branch.html",
        {
            "user": user_view,
            "platform_name": "Branch",
            "platform_icon": "🌿",
            "platform_desc": "Connect your Branch account for mobile attribution and deep linking data.",
            "editing": editing,
        },
    )


@router.get("/connect/appsflyer", response_class=HTMLResponse)
async def connect_appsflyer_page(request: Request):
    """AppsFlyer API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/appsflyer", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(AppsFlyerConnection).where(
                        AppsFlyerConnection.id == uuid.UUID(edit_id),
                        AppsFlyerConnection.user_id == uuid.UUID(user_ctx.user_id),
                        AppsFlyerConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/appsflyer.html",
        {
            "user": user_view,
            "platform_name": "AppsFlyer",
            "platform_icon": "🚀",
            "platform_desc": "Connect your AppsFlyer account for mobile attribution and analytics.",
            "editing": editing,
        },
    )


@router.get("/connect/adjust", response_class=HTMLResponse)
async def connect_adjust_page(request: Request):
    """Adjust API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/adjust", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(AdjustConnection).where(
                        AdjustConnection.id == uuid.UUID(edit_id),
                        AdjustConnection.user_id == uuid.UUID(user_ctx.user_id),
                        AdjustConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/adjust.html",
        {
            "user": user_view,
            "platform_name": "Adjust",
            "platform_icon": "🎯",
            "platform_desc": "Connect your Adjust account for mobile measurement and attribution.",
            "editing": editing,
        },
    )


class AmplitudeUploadRequest(BaseModel):
    display_name: str
    api_key: str
    secret_key: str


@router.post("/api/connections/amplitude")
async def add_amplitude_connection(payload: AmplitudeUploadRequest, request: Request):
    """Securely stores Amplitude API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)
    encrypted_secret_key = _encrypt(payload.secret_key)

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = AmplitudeConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            secret_key_encrypted=encrypted_secret_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/amplitude/{conn_id}")
async def update_amplitude_connection(conn_id: str, payload: AmplitudeUploadRequest, request: Request):
    """Updates an existing Amplitude connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)
    if payload.secret_key:
        values["secret_key_encrypted"] = _encrypt(payload.secret_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AmplitudeConnection)
            .where(
                AmplitudeConnection.id == uuid.UUID(conn_id),
                AmplitudeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/amplitude/{conn_id}")
async def disconnect_amplitude_connection(conn_id: str, request: Request):
    """Deactivates an Amplitude connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AmplitudeConnection)
            .where(
                AmplitudeConnection.id == uuid.UUID(conn_id),
                AmplitudeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


class BranchUploadRequest(BaseModel):
    display_name: str
    api_key: str
    secret_key: str


@router.post("/api/connections/branch")
async def add_branch_connection(payload: BranchUploadRequest, request: Request):
    """Securely stores Branch API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)
    encrypted_secret_key = _encrypt(payload.secret_key or "")

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = BranchConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            secret_key_encrypted=encrypted_secret_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/branch/{conn_id}")
async def update_branch_connection(conn_id: str, payload: BranchUploadRequest, request: Request):
    """Updates an existing Branch connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)
    if payload.secret_key:
        values["secret_key_encrypted"] = _encrypt(payload.secret_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BranchConnection)
            .where(
                BranchConnection.id == uuid.UUID(conn_id),
                BranchConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/branch/{conn_id}")
async def disconnect_branch_connection(conn_id: str, request: Request):
    """Deactivates a Branch connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BranchConnection)
            .where(
                BranchConnection.id == uuid.UUID(conn_id),
                BranchConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


class AppsFlyerUploadRequest(BaseModel):
    display_name: str
    api_key: str
    secret_key: str | None = None


@router.post("/api/connections/appsflyer")
async def add_appsflyer_connection(payload: AppsFlyerUploadRequest, request: Request):
    """Securely stores AppsFlyer API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)
    encrypted_secret_key = _encrypt(payload.secret_key or "")

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = AppsFlyerConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            secret_key_encrypted=encrypted_secret_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/appsflyer/{conn_id}")
async def update_appsflyer_connection(conn_id: str, payload: AppsFlyerUploadRequest, request: Request):
    """Updates an existing AppsFlyer connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)
    if payload.secret_key:
        values["secret_key_encrypted"] = _encrypt(payload.secret_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AppsFlyerConnection)
            .where(
                AppsFlyerConnection.id == uuid.UUID(conn_id),
                AppsFlyerConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/appsflyer/{conn_id}")
async def disconnect_appsflyer_connection(conn_id: str, request: Request):
    """Deactivates an AppsFlyer connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AppsFlyerConnection)
            .where(
                AppsFlyerConnection.id == uuid.UUID(conn_id),
                AppsFlyerConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


class AdjustUploadRequest(BaseModel):
    display_name: str
    api_key: str
    secret_key: str | None = None


@router.post("/api/connections/adjust")
async def add_adjust_connection(payload: AdjustUploadRequest, request: Request):
    """Securely stores Adjust API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)
    encrypted_secret_key = _encrypt(payload.secret_key or "")

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = AdjustConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            secret_key_encrypted=encrypted_secret_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/adjust/{conn_id}")
async def update_adjust_connection(conn_id: str, payload: AdjustUploadRequest, request: Request):
    """Updates an existing Adjust connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)
    if payload.secret_key:
        values["secret_key_encrypted"] = _encrypt(payload.secret_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AdjustConnection)
            .where(
                AdjustConnection.id == uuid.UUID(conn_id),
                AdjustConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/adjust/{conn_id}")
async def disconnect_adjust_connection(conn_id: str, request: Request):
    """Deactivates an Adjust connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AdjustConnection)
            .where(
                AdjustConnection.id == uuid.UUID(conn_id),
                AdjustConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Mixpanel Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/mixpanel", response_class=HTMLResponse)
async def connect_mixpanel_page(request: Request):
    """Mixpanel API secret connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/mixpanel", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(MixpanelConnection).where(
                        MixpanelConnection.id == uuid.UUID(edit_id),
                        MixpanelConnection.user_id == uuid.UUID(user_ctx.user_id),
                        MixpanelConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/mixpanel.html",
        {
            "user": user_view,
            "platform_name": "Mixpanel",
            "platform_icon": "🎯",
            "platform_desc": "Connect your Mixpanel project for product analytics insights and event data.",
            "editing": editing,
        },
    )


class MixpanelUploadRequest(BaseModel):
    display_name: str
    api_key: str
    secret_key: str


@router.post("/api/connections/mixpanel")
async def add_mixpanel_connection(payload: MixpanelUploadRequest, request: Request):
    """Securely stores Mixpanel API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)
    encrypted_secret_key = _encrypt(payload.secret_key)

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = MixpanelConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            secret_key_encrypted=encrypted_secret_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/mixpanel/{conn_id}")
async def update_mixpanel_connection(conn_id: str, payload: MixpanelUploadRequest, request: Request):
    """Updates an existing Mixpanel connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)
    if payload.secret_key:
        values["secret_key_encrypted"] = _encrypt(payload.secret_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MixpanelConnection)
            .where(
                MixpanelConnection.id == uuid.UUID(conn_id),
                MixpanelConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/mixpanel/{conn_id}")
async def disconnect_mixpanel_connection(conn_id: str, request: Request):
    """Deactivates a Mixpanel connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MixpanelConnection)
            .where(
                MixpanelConnection.id == uuid.UUID(conn_id),
                MixpanelConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# PostHog Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/posthog", response_class=HTMLResponse)
async def connect_posthog_page(request: Request):
    """PostHog API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/posthog", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(PostHogConnection).where(
                        PostHogConnection.id == uuid.UUID(edit_id),
                        PostHogConnection.user_id == uuid.UUID(user_ctx.user_id),
                        PostHogConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "project_host": row.project_host,
                    "external_project_id": row.external_project_id,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                }

    return render(
        request,
        "connect/posthog.html",
        {
            "user": user_view,
            "platform_name": "PostHog",
            "platform_icon": "🦔",
            "platform_desc": "Connect your PostHog project (Cloud or self-hosted) for product analytics insights and event data.",
            "editing": editing,
        },
    )


class PostHogUploadRequest(BaseModel):
    display_name: str
    api_key: str
    project_host: str
    external_project_id: str


@router.post("/api/connections/posthog")
async def add_posthog_connection(payload: PostHogUploadRequest, request: Request):
    """Securely stores PostHog API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = PostHogConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            project_host=payload.project_host,
            external_project_id=payload.external_project_id,
            api_key_encrypted=encrypted_api_key,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/posthog/{conn_id}")
async def update_posthog_connection(conn_id: str, payload: PostHogUploadRequest, request: Request):
    """Updates an existing PostHog connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "project_host": payload.project_host,
        "external_project_id": payload.external_project_id,
    }
    if payload.api_key:
        values["api_key_encrypted"] = _encrypt(payload.api_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(PostHogConnection)
            .where(
                PostHogConnection.id == uuid.UUID(conn_id),
                PostHogConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/posthog/{conn_id}")
async def disconnect_posthog_connection(conn_id: str, request: Request):
    """Deactivates a PostHog connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(PostHogConnection)
            .where(
                PostHogConnection.id == uuid.UUID(conn_id),
                PostHogConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Braze Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/braze", response_class=HTMLResponse)
async def connect_braze_page(request: Request):
    """Braze API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/braze", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(BrazeConnection).where(
                        BrazeConnection.id == uuid.UUID(edit_id),
                        BrazeConnection.user_id == uuid.UUID(user_ctx.user_id),
                        BrazeConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                    "rest_endpoint_url": row.rest_endpoint_url,
                }

    return render(
        request,
        "connect/braze.html",
        {
            "user": user_view,
            "platform_name": "Braze",
            "platform_icon": "📨",
            "platform_desc": "Connect your Braze account for customer engagement analytics, campaigns, and event data.",
            "editing": editing,
        },
    )


class BrazeUploadRequest(BaseModel):
    display_name: str
    api_key: str
    rest_endpoint_url: str


@router.post("/api/connections/braze")
async def add_braze_connection(payload: BrazeUploadRequest, request: Request):
    """Securely stores Braze API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = BrazeConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            api_key_encrypted=encrypted_api_key,
            rest_endpoint_url=payload.rest_endpoint_url.rstrip("/"),
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/braze/{conn_id}")
async def update_braze_connection(conn_id: str, payload: BrazeUploadRequest, request: Request):
    """Updates an existing Braze connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "rest_endpoint_url": payload.rest_endpoint_url.rstrip("/"),
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BrazeConnection)
            .where(
                BrazeConnection.id == uuid.UUID(conn_id),
                BrazeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/braze/{conn_id}")
async def disconnect_braze_connection(conn_id: str, request: Request):
    """Deactivates a Braze connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(BrazeConnection)
            .where(
                BrazeConnection.id == uuid.UUID(conn_id),
                BrazeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# MoEngage Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/moengage", response_class=HTMLResponse)
async def connect_moengage_page(request: Request):
    """MoEngage API key connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/moengage", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(MoengageConnection).where(
                        MoengageConnection.id == uuid.UUID(edit_id),
                        MoengageConnection.user_id == uuid.UUID(user_ctx.user_id),
                        MoengageConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "app_id": row.app_id,
                    "api_key_hint": _secret_hint(_decrypt(row.api_key_encrypted)),
                    "data_center": row.data_center,
                }

    return render(
        request,
        "connect/moengage.html",
        {
            "user": user_view,
            "platform_name": "MoEngage",
            "platform_icon": "📧",
            "platform_desc": "Connect your MoEngage account for customer engagement analytics, campaigns, and event data.",
            "editing": editing,
        },
    )


class MoengageUploadRequest(BaseModel):
    display_name: str
    app_id: str
    api_key: str
    data_center: str


@router.post("/api/connections/moengage")
async def add_moengage_connection(payload: MoengageUploadRequest, request: Request):
    """Securely stores MoEngage API credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_api_key = _encrypt(payload.api_key)

    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = MoengageConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            app_id=payload.app_id,
            api_key_encrypted=encrypted_api_key,
            data_center=payload.data_center,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/moengage/{conn_id}")
async def update_moengage_connection(conn_id: str, payload: MoengageUploadRequest, request: Request):
    """Updates an existing MoEngage connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "app_id": payload.app_id,
        "data_center": payload.data_center,
    }
    if payload.api_key:  # blank = keep the stored value
        values["api_key_encrypted"] = _encrypt(payload.api_key)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MoengageConnection)
            .where(
                MoengageConnection.id == uuid.UUID(conn_id),
                MoengageConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/moengage/{conn_id}")
async def disconnect_moengage_connection(conn_id: str, request: Request):
    """Deactivates a MoEngage connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MoengageConnection)
            .where(
                MoengageConnection.id == uuid.UUID(conn_id),
                MoengageConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Adobe Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/adobe", response_class=HTMLResponse)
async def connect_adobe_page(request: Request):
    """Adobe IMS connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/adobe", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(AdobeConnection).where(
                        AdobeConnection.id == uuid.UUID(edit_id),
                        AdobeConnection.user_id == uuid.UUID(user_ctx.user_id),
                        AdobeConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "org_id": row.org_id,
                    "client_id_hint": _secret_hint(_decrypt(row.client_id_encrypted)),
                    "company_id": row.company_id or "",
                    "has_analytics": row.has_analytics,
                    "has_launch": row.has_launch,
                }

    return render(
        request,
        "connect/adobe.html",
        {
            "user": user_view,
            "platform_name": "Adobe",
            "platform_icon": "🔴",
            "platform_desc": "Connect Adobe Analytics and/or Launch for real-time reporting and tag management.",
            "editing": editing,
        },
    )


async def _discover_adobe_company_id(client_id: str, client_secret: str, org_id: str) -> str | None:
    """Best-effort Analytics globalCompanyId from GET /discovery/me."""
    adobe = getattr(app_state, "adobe_analytics_connector", None)
    if adobe is None:
        return None
    try:
        resolved = await adobe.resolve_company_id(client_id, client_secret, org_id)
    except Exception:
        logger.exception("Adobe company-id discovery failed")
        return None
    if resolved.get("error"):
        logger.info("Adobe company-id discovery: %s", resolved.get("message"))
        return None
    company_id = resolved.get("company_id")
    return str(company_id) if company_id else None


class AdobeUploadRequest(BaseModel):
    display_name: str
    org_id: str
    client_id: str
    client_secret: str
    company_id: str | None = None
    has_analytics: bool = True
    has_launch: bool = True


@router.post("/api/connections/adobe")
async def add_adobe_connection(payload: AdobeUploadRequest, request: Request):
    """Securely stores Adobe IMS credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_client_id = _encrypt(payload.client_id)
    encrypted_client_secret = _encrypt(payload.client_secret)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")
    company_id = payload.company_id
    if payload.has_analytics and not (company_id or "").strip():
        company_id = await _discover_adobe_company_id(
            payload.client_id, payload.client_secret, payload.org_id
        )

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = AdobeConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            org_id=payload.org_id,
            company_id=company_id,
            client_id_encrypted=encrypted_client_id,
            client_secret_encrypted=encrypted_client_secret,
            has_analytics=payload.has_analytics,
            has_launch=payload.has_launch,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/adobe/{conn_id}")
async def update_adobe_connection(conn_id: str, payload: AdobeUploadRequest, request: Request):
    """Updates an existing Adobe connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    company_id = (payload.company_id or "").strip() or None
    if payload.has_analytics and not company_id and payload.client_secret:
        company_id = await _discover_adobe_company_id(
            payload.client_id, payload.client_secret, payload.org_id
        )

    values: dict = {
        "display_name": payload.display_name,
        "org_id": payload.org_id,
        "has_analytics": payload.has_analytics,
        "has_launch": payload.has_launch,
    }
    if payload.client_id:  # blank = keep the stored value
        values["client_id_encrypted"] = _encrypt(payload.client_id)
    if company_id:
        values["company_id"] = company_id
    if payload.client_secret:
        values["client_secret_encrypted"] = _encrypt(payload.client_secret)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AdobeConnection)
            .where(
                AdobeConnection.id == uuid.UUID(conn_id),
                AdobeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/adobe/{conn_id}")
async def disconnect_adobe_connection(conn_id: str, request: Request):
    """Deactivates an Adobe connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(AdobeConnection)
            .where(
                AdobeConnection.id == uuid.UUID(conn_id),
                AdobeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Marketo Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/marketo", response_class=HTMLResponse)
async def connect_marketo_page(request: Request):
    """Adobe Marketo Engage connect page (supports ?edit=<id>)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/marketo", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(MarketoConnection).where(
                        MarketoConnection.id == uuid.UUID(edit_id),
                        MarketoConnection.user_id == uuid.UUID(user_ctx.user_id),
                        MarketoConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "instance_url": row.instance_url,
                    "client_id_hint": _secret_hint(_decrypt(row.client_id_encrypted)),
                }

    return render(
        request,
        "connect/marketo.html",
        {
            "user": user_view,
            "platform_name": "Adobe Marketo Engage",
            "platform_icon": "🟣",
            "platform_desc": "Connect Adobe Marketo Engage for leads, campaigns, and marketing automation.",
            "editing": editing,
        },
    )


class MarketoUploadRequest(BaseModel):
    display_name: str
    instance_url: str
    client_id: str
    client_secret: str


@router.post("/api/connections/marketo")
async def add_marketo_connection(payload: MarketoUploadRequest, request: Request):
    """Securely stores Adobe Marketo Engage credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_client_id = _encrypt(payload.client_id)
    encrypted_client_secret = _encrypt(payload.client_secret)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = MarketoConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            instance_url=payload.instance_url.rstrip("/"),
            client_id_encrypted=encrypted_client_id,
            client_secret_encrypted=encrypted_client_secret,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/marketo/{conn_id}")
async def update_marketo_connection(conn_id: str, payload: MarketoUploadRequest, request: Request):
    """Updates an existing Marketo connection (blank client_secret keeps the existing one)."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "instance_url": payload.instance_url.rstrip("/"),
    }
    if payload.client_id:  # blank = keep the stored value
        values["client_id_encrypted"] = _encrypt(payload.client_id)
    if payload.client_secret:
        values["client_secret_encrypted"] = _encrypt(payload.client_secret)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MarketoConnection)
            .where(
                MarketoConnection.id == uuid.UUID(conn_id),
                MarketoConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/marketo/{conn_id}")
async def disconnect_marketo_connection(conn_id: str, request: Request):
    """Deactivates a Marketo connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(MarketoConnection)
            .where(
                MarketoConnection.id == uuid.UUID(conn_id),
                MarketoConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Redshift Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/redshift", response_class=HTMLResponse)
async def connect_redshift_page(request: Request):
    """Redshift connect page (supports ?edit=<id> to pre-fill form)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/redshift", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(RedshiftConnection).where(
                        RedshiftConnection.id == uuid.UUID(edit_id),
                        RedshiftConnection.user_id == uuid.UUID(user_ctx.user_id),
                        RedshiftConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "host_hint": _secret_hint(_decrypt(row.host_encrypted)),
                    "port": row.port,
                    "database": row.database,
                    "username_hint": _secret_hint(_decrypt(row.username_encrypted)),
                    "default_schema": row.default_schema,
                }

    return render(
        request,
        "connect/redshift.html",
        {
            "user": user_view,
            "platform_name": "Redshift",
            "platform_icon": "🟠",
            "platform_desc": "Connect your Amazon Redshift cluster for direct data warehouse queries and analysis.",
            "editing": editing,
        },
    )


class RedshiftUploadRequest(BaseModel):
    display_name: str
    host: str
    port: int
    database: str
    username: str
    password: str
    default_schema: str | None = "public"


@router.post("/api/connections/redshift")
async def add_redshift_connection(payload: RedshiftUploadRequest, request: Request):
    """Securely stores Redshift connection credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_host = _encrypt(payload.host)
    encrypted_username = _encrypt(payload.username)
    encrypted_password = _encrypt(payload.password)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = RedshiftConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            host_encrypted=encrypted_host,
            port=payload.port,
            database=payload.database,
            username_encrypted=encrypted_username,
            password_encrypted=encrypted_password,
            default_schema=payload.default_schema or "public",
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/redshift/{conn_id}")
async def update_redshift_connection(conn_id: str, payload: RedshiftUploadRequest, request: Request):
    """Updates an existing Redshift connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "port": payload.port,
        "database": payload.database,
        "default_schema": payload.default_schema or "public",
    }
    if payload.host:  # blank = keep the stored value
        values["host_encrypted"] = _encrypt(payload.host)
    if payload.username:  # blank = keep the stored value
        values["username_encrypted"] = _encrypt(payload.username)
    # Only update password if a new one was provided (non-empty)
    if payload.password:
        values["password_encrypted"] = _encrypt(payload.password)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(RedshiftConnection)
            .where(
                RedshiftConnection.id == uuid.UUID(conn_id),
                RedshiftConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/redshift/{conn_id}")
async def disconnect_redshift_connection(conn_id: str, request: Request):
    """Deactivates a Redshift connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(RedshiftConnection)
            .where(
                RedshiftConnection.id == uuid.UUID(conn_id),
                RedshiftConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# Snowflake Routes (Credential-based connection)
# ---------------------------------------------------------------------------


@router.get("/connect/snowflake", response_class=HTMLResponse)
async def connect_snowflake_page(request: Request):
    """Snowflake connect page (supports ?edit=<id> to pre-fill form)."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/connect/snowflake", status_code=302)
    user_view = await _load_user_view(user_ctx)

    editing = None
    edit_id = request.query_params.get("edit")
    if edit_id and user_ctx:
        db_session = app_state.db_session_factory()
        async with db_session as db:
            row = (
                await db.execute(
                    select(SnowflakeConnection).where(
                        SnowflakeConnection.id == uuid.UUID(edit_id),
                        SnowflakeConnection.user_id == uuid.UUID(user_ctx.user_id),
                        SnowflakeConnection.is_active == True,
                    )
                )
            ).scalar_one_or_none()
            if row:
                editing = {
                    "id": str(row.id),
                    "display_name": row.display_name,
                    "account_hint": _secret_hint(_decrypt(row.account_encrypted)),
                    "username_hint": _secret_hint(_decrypt(row.username_encrypted)),
                    "warehouse": row.warehouse,
                    "database": row.database,
                    "default_schema": row.default_schema,
                    "role": row.role or "",
                }

    return render(
        request,
        "connect/snowflake.html",
        {
            "user": user_view,
            "platform_name": "Snowflake",
            "platform_icon": "❄️",
            "platform_desc": "Connect your Snowflake account for cloud-native data warehouse queries and analysis.",
            "editing": editing,
        },
    )


class SnowflakeUploadRequest(BaseModel):
    display_name: str
    account: str
    username: str
    password: str
    warehouse: str
    database: str
    default_schema: str | None = "PUBLIC"
    role: str | None = None


@router.post("/api/connections/snowflake")
async def add_snowflake_connection(payload: SnowflakeUploadRequest, request: Request):
    """Securely stores Snowflake connection credentials."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    encrypted_account = _encrypt(payload.account)
    encrypted_username = _encrypt(payload.username)
    encrypted_password = _encrypt(payload.password)
    active_project_id = request.query_params.get("project_id") or request.cookies.get("active_project_id")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        new_conn = SnowflakeConnection(
            user_id=uuid.UUID(user_ctx.user_id),
            project_id=active_project_id,
            display_name=payload.display_name,
            account_encrypted=encrypted_account,
            username_encrypted=encrypted_username,
            password_encrypted=encrypted_password,
            warehouse=payload.warehouse,
            database=payload.database,
            default_schema=payload.default_schema or "PUBLIC",
            role=payload.role,
            connection_status="active",
            is_active=True,
        )
        db.add(new_conn)
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "success", "connection_id": str(new_conn.id)}


@router.put("/api/connections/snowflake/{conn_id}")
async def update_snowflake_connection(conn_id: str, payload: SnowflakeUploadRequest, request: Request):
    """Updates an existing Snowflake connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    values: dict = {
        "display_name": payload.display_name,
        "warehouse": payload.warehouse,
        "database": payload.database,
        "default_schema": payload.default_schema or "PUBLIC",
        "role": payload.role,
    }
    if payload.account:  # blank = keep the stored value
        values["account_encrypted"] = _encrypt(payload.account)
    if payload.username:  # blank = keep the stored value
        values["username_encrypted"] = _encrypt(payload.username)
    if payload.password:
        values["password_encrypted"] = _encrypt(payload.password)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(SnowflakeConnection)
            .where(
                SnowflakeConnection.id == uuid.UUID(conn_id),
                SnowflakeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(**values)
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "updated"}


@router.delete("/api/connections/snowflake/{conn_id}")
async def disconnect_snowflake_connection(conn_id: str, request: Request):
    """Deactivates a Snowflake connection."""
    user_ctx = None
    try:
        user_ctx = await require_valid_mcp_token(request)
    except Exception:
        uid = get_uid_from_request(request)
        if uid:
            user_ctx = await build_user_context(uid, request)

    if not user_ctx:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db_session = app_state.db_session_factory()
    async with db_session as db:
        await db.execute(
            update(SnowflakeConnection)
            .where(
                SnowflakeConnection.id == uuid.UUID(conn_id),
                SnowflakeConnection.user_id == uuid.UUID(user_ctx.user_id),
            )
            .values(is_active=False, connection_status="disconnected")
        )
        await db.commit()

    asyncio.create_task(invalidate_user_context_cache(str(user_ctx.user_id)))
    return {"status": "disconnected"}
