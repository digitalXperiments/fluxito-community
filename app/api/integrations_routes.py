"""Integrations API — manage install-wide OAuth app credentials.

GET    /api/integrations               — list all 6 platforms with status
GET    /api/integrations/<platform>    — single platform details
POST   /api/integrations/<platform>    — upsert credentials
DELETE /api/integrations/<platform>    — remove (env fallback resumes)
POST   /api/integrations/<platform>/test — stub validation

All endpoints require install-admin role: project-`owner` or project-`admin`
of any project the user belongs to.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.app_state as app_state
from app.auth.oauth_app_credentials import (
    OAuthAppNotConfigured,
    SUPPORTED_PLATFORMS,
    delete_oauth_app_credentials,
    get_oauth_app_credentials,
    list_oauth_app_status,
    upsert_oauth_app_credentials,
)
from app.config import settings
from app.models.project import ProjectMember
from app.models.user import User
from app.settings_service import (
    RUNTIME_SETTING_BY_KEY,
    delete_setting,
    list_runtime_settings,
    set_setting,
)
from app.templating import render

router = APIRouter()


DEV_CONSOLE_URL = {
    "google": "https://console.cloud.google.com/apis/credentials",
    "meta": "https://developers.facebook.com/apps",
    "tiktok": "https://business-api.tiktok.com/portal",
    "snap": "https://kit.snapchat.com/manage/apps",
    "linkedin": "https://www.linkedin.com/developers/apps",
    "pinterest": "https://developers.pinterest.com/apps/",
    "x": "https://developer.x.com/en/portal/projects-and-apps",
    "reddit": "https://www.reddit.com/prefs/apps",
    "bing": "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
    "apple": "https://ads.apple.com/cm/app/settings/api",
}

# Maps each OAuth platform to its tutorial markdown file(s).
# Google is special: one foundational setup + per-product guides.
PLATFORM_TUTORIALS: dict[str, list[str]] = {
    "google": [
        "google-cloud-setup",
        "google-analytics-4",
        "google-tag-manager",
        "google-ads",
        "search-console",
        "bigquery",
    ],
    "meta": ["meta-ads"],
    "tiktok": ["tiktok-ads"],
    "snap": ["snap-ads"],
    "linkedin": ["linkedin-ads"],
    "pinterest": ["pinterest-ads"],
    "x": ["x-ads"],
    "reddit": ["reddit-ads"],
    "bing": ["bing-webmaster"],
    "apple": ["apple-ads"],
}


def _redirect_uris_for(platform: str) -> list[str]:
    base = (settings.APP_BASE_URL or "http://localhost:8000").rstrip("/")
    by_platform: dict[str, list[str]] = {
        "google": [
            f"{base}/auth/google/identity/callback",
            f"{base}/auth/google/data/callback",
            f"{base}/auth/google/signin/callback",
        ],
        "meta": [f"{base}/auth/meta/callback"],
        "tiktok": [f"{base}/auth/tiktok/callback"],
        "snap": [f"{base}/auth/snap/callback"],
        "linkedin": [f"{base}/auth/linkedin/callback"],
        "pinterest": [f"{base}/auth/pinterest/callback"],
        "x": [f"{base}/auth/x/callback"],
        "reddit": [f"{base}/auth/reddit/callback"],
        "bing": [f"{base}/auth/bing/callback"],
        "apple": [],
    }
    return by_platform[platform]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_user(request: Request) -> User | None:
    """Resolve authenticated user from cookie or MCP bearer token.

    Mirrors the pattern used in project_routes.py / google_oauth_routes.py:
    call ``_resolve_user_ctx`` to obtain a UserContext, then load the User row.
    """
    from app.api.google_oauth_routes import _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    if user_ctx is None:
        return None

    try:
        user_uuid = uuid.UUID(user_ctx.user_id)
    except (ValueError, AttributeError):
        return None

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_uuid))
        return result.scalar_one_or_none()


async def _is_install_admin(db: AsyncSession, user: User) -> bool:
    """Install admin = super admin. OAuth app credentials are instance-wide.

    Owning a project is NOT enough: every self-serve signup owns its own
    workspace, so a project-role check would let any user rewrite the client
    secrets everyone connects through. Bootstrap fallback: on an install with
    no super admin at all and a single user (a first user who arrived via
    Google sign-in before migration 044's backfill), that user's project
    owner/admin role still gets them in so the instance can be configured.
    """
    if user.is_superadmin:
        return True
    any_superadmin = (
        await db.execute(select(User.id).where(User.is_superadmin.is_(True)).limit(1))
    ).scalar_one_or_none() is not None
    if any_superadmin:
        return False
    # Bootstrap only: a brand-new single-user install. With more than one user,
    # "owns a project" is true of every signup, so it can't mean install admin.
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    if user_count != 1:
        return False
    return (
        await db.execute(
            select(ProjectMember.id)
            .where(ProjectMember.user_id == user.id)
            .where(ProjectMember.role.in_(("owner", "admin")))
            .where(ProjectMember.is_active.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _require_install_admin(request: Request, db: AsyncSession) -> User:
    """Resolve the current user and verify they are an install (super) admin.

    Raises HTTPException 401 if not signed in, 403 if signed in but not an
    install admin.
    """
    user = await _resolve_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not await _is_install_admin(db, user):
        raise HTTPException(status_code=403, detail="Install admin role required")

    return user


def _validate_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform!r}")


def _mask(client_id: str) -> str:
    if not client_id:
        return ""
    if len(client_id) <= 12:
        return "***"
    return f"{client_id[:6]}…{client_id[-4:]}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/integrations")
async def list_integrations(request: Request):
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        items = await list_oauth_app_status(db)
    return JSONResponse({"items": items})


@router.get("/api/integrations/{platform}")
async def get_integration(request: Request, platform: str):
    _validate_platform(platform)
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        try:
            creds = await get_oauth_app_credentials(db, platform)
            configured = True
            source = creds.source
            client_id_masked = _mask(creds.client_id)
        except OAuthAppNotConfigured:
            configured = False
            source = "unconfigured"
            client_id_masked = None

    return JSONResponse(
        {
            "platform": platform,
            "configured": configured,
            "source": source,
            "client_id_masked": client_id_masked,
            "redirect_uris": _redirect_uris_for(platform),
            "dev_console_url": DEV_CONSOLE_URL[platform],
            "tutorial_slugs": PLATFORM_TUTORIALS.get(platform, []),
        }
    )


def _load_tutorial(slug: str) -> tuple[str, str]:
    """Load a tutorial markdown file. Returns (title, html_content).

    Raises HTTPException on bad slug or missing file.
    """
    import re
    from pathlib import Path

    import markdown as md

    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,60}", slug):
        raise HTTPException(status_code=400, detail="Invalid tutorial slug")

    tutorial_dir = Path(__file__).resolve().parent.parent.parent / "docs" / "tutorials"
    filepath = tutorial_dir / f"{slug}.md"

    try:
        filepath.resolve().relative_to(tutorial_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tutorial slug")

    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Tutorial not found")

    raw = filepath.read_text(encoding="utf-8")

    # Extract H1 title
    lines = raw.split("\n")
    title = slug.replace("-", " ").title()
    if lines and lines[0].startswith("# "):
        title = lines[0].lstrip("# ").strip()
        lines = lines[1:]

    html_body = md.markdown(
        "\n".join(lines),
        extensions=["tables", "fenced_code"],
    )
    html_body = re.sub(
        r'href="([a-z0-9][a-z0-9\-]{0,60})\.md"',
        r'href="/tutorials/\1"',
        html_body,
    )
    return title, html_body


# Tutorial metadata for the index page.
#
# Tutorials in the first two categories ("Start here" / "Use Fluxito
# features") carry a 5th `interactive` bool: it flags a guide that walks the
# user through a live, step-by-step flow (vs. a plain reference doc), and
# drives the "INTERACTIVE" badge + dark-card treatment on the tutorials index
# featured grid. Every other category's tuples stay 4-wide (slug, title,
# time, note) — `tutorials/platforms.html` unpacks those positionally.
TUTORIAL_CATEGORIES = [
    {
        "title": "Start here",
        "description": "Project setup, MCP connection, and the context the AI needs before it operates.",
        "tutorials": [
            (
                "projects-and-connections",
                "Projects and connections",
                "~8 min",
                "Create projects and connect accounts",
                False,
            ),
            (
                "connect-ai-mcp",
                "Connect an AI with MCP",
                "~10 min",
                "Add Fluxito to Claude, ChatGPT, Cursor, or another MCP client",
                True,
            ),
            (
                "fluxito-skill",
                "Add the Fluxito Skill",
                "~5 min",
                "Install the Agent Skill so your AI operates Fluxito the right way",
                False,
            ),
            (
                "platform-setup-guides",
                "Platform setup guides",
                "~10 min",
                "Pick the right connector guide for each data source",
                False,
            ),
            (
                "business-context",
                "Business Context",
                "~10 min",
                "Teach the AI your business rules and terminology",
                False,
            ),
        ],
    },
    {
        "title": "Use Fluxito features",
        "description": "Guides for the core workflows: KPIs and audits.",
        "tutorials": [
            (
                "kpi-library",
                "KPI Library",
                "~15 min",
                "Define metrics, formulas, owners, and targets",
                False,
            ),
            (
                "audits-and-activity",
                "Audits and Activity Log",
                "~12 min",
                "Run health checks and review AI tool calls",
                False,
            ),
        ],
    },
    {
        "title": "Google platforms",
        "description": "One OAuth app covers GA4, GTM, Ads, Search Console, and BigQuery.",
        "tutorials": [
            ("google-cloud-setup", "Google Cloud setup", "~20 min", "Foundational — create the OAuth app"),
            ("google-analytics-4", "Google Analytics 4", "~10 min", "GA4 property access"),
            ("google-tag-manager", "Google Tag Manager", "~10 min", "Container read/write"),
            ("google-ads", "Google Ads", "~15 min", "Requires developer token approval"),
            ("search-console", "Search Console", "~10 min", "SEO data"),
            ("bigquery", "BigQuery", "~15 min", "Warehouse queries"),
        ],
    },
    {
        "title": "Paid media",
        "description": "Each platform needs its own OAuth app registered in the vendor's developer console.",
        "tutorials": [
            ("meta-ads", "Meta Ads", "~20 min", "Facebook & Instagram ads"),
            ("tiktok-ads", "TikTok Ads", "~20 min", "Requires app review"),
            ("linkedin-ads", "LinkedIn Ads", "~20 min", "Requires app review"),
            ("pinterest-ads", "Pinterest Ads", "~15 min", "Requires app review"),
            ("x-ads", "X Ads", "~20 min", "Uses OAuth 1.0a and Ads API access"),
            ("apple-ads", "Apple Ads", "~15 min", "Uses OAuth 2 client credentials"),
            ("snap-ads", "Snap Ads", "~20 min", "Snapchat marketing API"),
        ],
    },
    {
        "title": "Data warehouses",
        "description": "Credential-based connections — no OAuth app needed.",
        "tutorials": [
            ("snowflake", "Snowflake", "~15 min", "Service account setup"),
            ("redshift", "Redshift", "~20 min", "IAM or password auth"),
        ],
    },
    {
        "title": "Analytics & tag management",
        "description": "Additional analytics platforms and tag managers.",
        "tutorials": [
            ("amplitude", "Amplitude", "~5 min", "API key + secret"),
            ("branch", "Branch", "~5 min", "API key + secret"),
            ("appsflyer", "AppsFlyer", "~5 min", "Master API token"),
            ("adjust", "Adjust", "~5 min", "Adjust API token"),
            ("mixpanel", "Mixpanel", "~5 min", "API secret + service token"),
            ("posthog", "PostHog", "~5 min", "API key + project host"),
            ("braze", "Braze", "~5 min", "API key + REST endpoint"),
            ("moengage", "MoEngage", "~5 min", "App ID + API key + data center"),
            ("adobe-analytics", "Adobe Analytics", "~25 min", "Adobe I/O project"),
            ("adobe-launch", "Adobe Launch", "~15 min", "Shares Adobe I/O credentials"),
            ("adobe-marketo", "Adobe Marketo Engage", "~15 min", "Marketo LaunchPoint service"),
        ],
    },
]


def _tutorial_page_context(slug: str) -> dict:
    tutorial_count = sum(len(category["tutorials"]) for category in TUTORIAL_CATEGORIES)
    for category in TUTORIAL_CATEGORIES:
        for tutorial in category["tutorials"]:
            if tutorial[0] == slug:
                return {
                    "tutorial_count": tutorial_count,
                    "current_tutorial": tutorial,
                    "current_category": category,
                    "related_tutorials": [item for item in category["tutorials"] if item[0] != slug][:4],
                }
    return {
        "tutorial_count": tutorial_count,
        "current_tutorial": None,
        "current_category": None,
        "related_tutorials": [],
    }


def _top_level_tutorials() -> list[tuple[str, str, str, str, bool]]:
    return [item for category in TUTORIAL_CATEGORIES[:2] for item in category["tutorials"]]


def _platform_tutorial_categories() -> list[dict]:
    return TUTORIAL_CATEGORIES[2:]


def _platform_tutorials() -> list[tuple[str, str, str, str]]:
    return [item for category in _platform_tutorial_categories() for item in category["tutorials"]]


@router.get("/tutorials", response_class=HTMLResponse)
async def tutorials_index(request: Request):
    """Tutorials index page — categorized list of all setup guides."""
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    user_view = await _load_user_view(user_ctx) if user_ctx else None

    return render(
        request,
        "tutorials/index.html",
        {
            "user": user_view,
            "tutorials": _top_level_tutorials(),
            "tutorial_count": len(_top_level_tutorials()),
            "tutorial_categories": TUTORIAL_CATEGORIES,
            "active": "tutorials",
        },
    )


@router.get("/tutorials/{slug}", response_class=HTMLResponse)
async def tutorial_page(request: Request, slug: str):
    """Render a single tutorial as an HTML page."""
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    user_view = await _load_user_view(user_ctx) if user_ctx else None

    if slug == "platform-setup-guides":
        return render(
            request,
            "tutorials/platforms.html",
            {
                "user": user_view,
                "title": "Platform Setup Guides",
                "platform_categories": _platform_tutorial_categories(),
                "platform_tutorials": _platform_tutorials(),
                "tutorial_count": len(_platform_tutorials()),
                "active": "tutorials",
            },
        )

    title, html_body = _load_tutorial(slug)

    return render(
        request,
        "tutorials/detail.html",
        {
            "user": user_view,
            "title": title,
            "slug": slug,
            "tutorial_html": html_body,
            **_tutorial_page_context(slug),
            "active": "tutorials",
        },
    )


@router.post("/api/integrations/{platform}")
async def upsert_integration(request: Request, platform: str):
    _validate_platform(platform)
    payload = await request.json()
    client_id = (payload.get("client_id") or "").strip()
    client_secret = (payload.get("client_secret") or "").strip()
    extra = payload.get("extra") or None

    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required")
    if not isinstance(client_id, str) or len(client_id) > 255:
        raise HTTPException(status_code=400, detail="client_id must be a string ≤ 255 chars")
    if not isinstance(client_secret, str) or len(client_secret) < 4:
        raise HTTPException(status_code=400, detail="client_secret looks too short")

    async with app_state.db_session_factory() as db:
        user = await _require_install_admin(request, db)
        await upsert_oauth_app_credentials(
            db,
            platform=platform,
            client_id=client_id,
            client_secret=client_secret,
            extra=extra if isinstance(extra, dict) else None,
            configured_by_user_id=user.id,
        )
        await db.commit()

    return JSONResponse({"success": True, "platform": platform})


@router.delete("/api/integrations/{platform}")
async def delete_integration(request: Request, platform: str):
    _validate_platform(platform)
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        deleted = await delete_oauth_app_credentials(db, platform=platform)
        await db.commit()
    return JSONResponse({"success": True, "deleted": deleted})


@router.post("/api/integrations/{platform}/test")
async def test_integration(request: Request, platform: str):
    """Light-weight validation: confirm credentials are configured and look
    syntactically valid. Most platforms don't expose a no-code 'app exists'
    endpoint, so we keep this conservative.
    """
    _validate_platform(platform)
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        try:
            creds = await get_oauth_app_credentials(db, platform)
        except OAuthAppNotConfigured:
            raise HTTPException(status_code=404, detail=f"{platform} is not configured")

    issues: list[str] = []
    if not creds.client_id:
        issues.append("client_id is empty")
    if not creds.client_secret:
        issues.append("client_secret is empty")
    if len(creds.client_id) < 4:
        issues.append("client_id looks too short")
    if len(creds.client_secret) < 4:
        issues.append("client_secret looks too short")
    if platform == "google" and not (creds.extra or {}).get("developer_token"):
        issues.append("note: GOOGLE_ADS_DEVELOPER_TOKEN extra not set (only required for Google Ads)")

    ok = not any(i for i in issues if not i.startswith("note:"))
    return JSONResponse(
        {
            "platform": platform,
            "ok": ok,
            "source": creds.source,
            "issues": issues,
        }
    )


@router.get("/api/settings/system")
async def list_system_settings(request: Request):
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        items = await list_runtime_settings(db)
    return JSONResponse({"items": items})


@router.post("/api/settings/system/{key}")
async def upsert_system_setting(request: Request, key: str):
    spec = RUNTIME_SETTING_BY_KEY.get(key)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"Unsupported setting: {key!r}")

    payload = await request.json()
    value = payload.get("value")
    if spec.is_secret and (value is None or str(value) == ""):
        raise HTTPException(status_code=400, detail="Secret value cannot be blank")
    if spec.value_type == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Value must be an integer")
    elif spec.value_type == "float":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Value must be a number")
    elif value is None:
        value = ""
    else:
        value = str(value)

    async with app_state.db_session_factory() as db:
        user = await _require_install_admin(request, db)
        await set_setting(
            db,
            key=key,
            value=value,
            is_secret=spec.is_secret,
            updated_by_user_id=user.id,
        )
        await db.commit()

    return JSONResponse({"success": True, "key": key})


@router.delete("/api/settings/system/{key}")
async def reset_system_setting(request: Request, key: str):
    if key not in RUNTIME_SETTING_BY_KEY:
        raise HTTPException(status_code=400, detail=f"Unsupported setting: {key!r}")
    async with app_state.db_session_factory() as db:
        await _require_install_admin(request, db)
        deleted = await delete_setting(db, key=key)
        await db.commit()
    return JSONResponse({"success": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# Settings UI page
# ---------------------------------------------------------------------------


@router.get("/settings/integrations", response_class=HTMLResponse)
async def integrations_page_moved(request: Request):
    """OAuth apps moved from Settings into the Admin panel; keep old links working."""
    qs = request.url.query
    return RedirectResponse(url="/admin/oauth-apps" + (f"?{qs}" if qs else ""), status_code=302)


@router.get("/admin/oauth-apps", response_class=HTMLResponse)
async def integrations_page(request: Request):
    """Render the install-admin OAuth apps page. Redirects to homepage if unauthenticated or not install admin."""
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user = await _resolve_user(request)
    if user is None:
        return RedirectResponse(url="/", status_code=302)

    async with app_state.db_session_factory() as db:
        if not await _is_install_admin(db, user):
            return RedirectResponse(url="/home", status_code=302)

        items = await list_oauth_app_status(db)

    user_ctx = await _resolve_user_ctx(request)
    user_view = await _load_user_view(user_ctx) if user_ctx else None

    return render(
        request,
        "settings/integrations.html",
        {
            "user": user_view,
            "platforms": items,
            "active": "admin",
        },
    )


@router.get("/settings/system", response_class=HTMLResponse)
async def system_settings_page(request: Request):
    """The System settings page was retired (site revamp): per-project SMTP
    lives in Project settings → Notifications, connector rate limits in
    Project settings → API limits, and app throttling / operations / branding
    in the superadmin /admin console. Runtime settings stay editable via the
    /api/settings/system API (or env). Redirect old links to /admin, which
    enforces its own access gate."""
    return RedirectResponse(url="/admin", status_code=302)
