"""
Jinja2 templating setup for the unified Fluxito UI.

All user-facing HTML pages render through this module so they share the
base layout, design tokens, and navigation.

Performance optimizations:
  - asset_hash() for cache-busting static file URLs
  - In-memory caching of mtime hashes
  - Lazy template loading
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app._version import get_version as _get_version
from app.branding import announcement as _announcement_global
from app.branding import brand as _brand_global
from app.branding import chat_enabled as _chat_enabled_global
from app.branding import gtm_container_id as _gtm_global

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Default fallback hash if file is missing (e.g., development environment)
_DEFAULT_ASSET_HASH = "dev"

# ---------- Asset fingerprinting -----------------------------------------------

_asset_hash_cache: dict[str, tuple[float, str]] = {}


def _asset_hash(path: str) -> str:
    """Generate a short hash based on file modification time.

    Used in templates: /static/css/app.css?v={{ asset_hash('css/app.css') }}

    Benefits:
    - Cache-busting: Changes to assets are immediately picked up by browsers
    - Long-lived caching: Browser caches assets with unique query strings
    - Development-friendly: mtime-based so dev changes work without restart

    Args:
        path: Asset path relative to static dir (e.g., 'css/app.css')

    Returns:
        8-char hex hash for cache-busting URL query param
    """
    full_path = _STATIC_DIR / path
    try:
        mtime = os.path.getmtime(full_path)
    except OSError:
        logger.debug(f"Asset not found: {path}, using default hash")
        return _DEFAULT_ASSET_HASH

    cached = _asset_hash_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    h = hashlib.md5(str(mtime).encode()).hexdigest()[:8]
    _asset_hash_cache[path] = (mtime, h)
    return h


# ---------- Custom filters ---------------------------------------------------


def _fmt_number(value: Any) -> str:
    """Format numbers with thousands separators; pass strings through."""
    try:
        if value is None:
            return ""
        n = float(value)
        if n.is_integer():
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (TypeError, ValueError):
        return str(value) if value is not None else ""


def _fmt_date(value: Any, fmt: str = "%b %d, %Y") -> str:
    """Format a datetime, ISO string, or date; empty on failure."""
    if not value:
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime(fmt)
        from datetime import datetime

        s = str(value).split("+")[0].split(".")[0].rstrip("Z")
        return datetime.fromisoformat(s).strftime(fmt)
    except Exception:
        return str(value)


def _initials(value: Any) -> str:
    s = str(value or "")
    if not s:
        return "?"
    return s[:2].upper()


def _fmt_datetime(value: Any, fmt: str = "%b %d, %Y %H:%M") -> str:
    """Format a datetime with time component; empty on failure."""
    if not value:
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime(fmt)
        from datetime import datetime

        s = str(value).split("+")[0].split(".")[0].rstrip("Z")
        return datetime.fromisoformat(s).strftime(fmt)
    except Exception:
        return str(value)


templates.env.filters["fmt_number"] = _fmt_number
templates.env.filters["fmt_date"] = _fmt_date
templates.env.filters["fmt_datetime"] = _fmt_datetime
templates.env.filters["initials"] = _initials

# Make asset_hash available as a global function in all templates
templates.env.globals["asset_hash"] = _asset_hash

# Expose instance branding (name, logo, accent) to all templates
templates.env.globals["brand"] = _brand_global

# Expose the site-wide announcement banner to all templates
templates.env.globals["announcement"] = _announcement_global

# Expose the site-wide GTM container ID to all templates
templates.env.globals["gtm_container_id"] = _gtm_global

# Expose the in-app Chat enabled state to all templates
templates.env.globals["chat_enabled"] = _chat_enabled_global

# Expose the running app version to all templates
templates.env.globals["app_version"] = _get_version()


# ---------- Render helpers ---------------------------------------------------

# Maps the legacy per-page `active` nav key to its lifecycle "section" in the
# grouped sidebar (Home / Chat / Audit / Context / Settings). Used by `render()` to derive a default `section` for templates
# that don't set one explicitly. `None` means "no section highlight" (e.g.
# the Tutorials help affordance, which isn't a nav cluster).
SECTION_BY_ACTIVE: dict[str, str | None] = {
    "home": "home",
    "chat": "chat",
    "audits": "audit",
    "audit": "audit",
    "audit_flows": "audit",
    "audit_vendors": "audit",
    "context": "context",
    "kpi_library": "context",
    "business_context": "context",
    "connect": "settings",
    "connections": "settings",
    "settings": "settings",
    "integrations": "settings",
    "system_settings": "settings",
    "profile": "settings",
    "projects": "settings",
    "admin": "settings",
    "tutorials": None,
}


def render(
    request: Request,
    template_name: str,
    context: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template with sane defaults (request, user, active) injected."""
    ctx = dict(context or {})
    ctx.setdefault("request", request)
    ctx.setdefault("user", None)
    ctx.setdefault("active", None)
    ctx.setdefault("section", SECTION_BY_ACTIVE.get(ctx.get("active")))
    ctx.setdefault("base_url", _base_url_from_request(request))
    # Inject project context for the nav switcher
    if "active_project_name" not in ctx:
        ctx["active_project_name"] = getattr(request.state, "active_project_name", None)
    if "active_project_id" not in ctx:
        ctx["active_project_id"] = getattr(request.state, "active_project_id", None)
    if "active_project_plan" not in ctx:
        ctx["active_project_plan"] = getattr(request.state, "active_project_plan", "free")
    if "user_project_role" not in ctx:
        ctx["user_project_role"] = getattr(request.state, "active_project_role", None)
    if "nav_projects" not in ctx:
        ctx["nav_projects"] = getattr(request.state, "nav_projects", [])
    # Sidebar "Flux's tasks" badge count — populated by the
    # ``_attach_nav_project_context`` ASGI middleware in main.py (same
    # choke point as nav_projects/active_project_*), which counts active
    # AutomationInstallation rows for the resolved active project. 0/absent
    # hides the badge.
    if "sidebar_tasks_count" not in ctx:
        ctx["sidebar_tasks_count"] = getattr(request.state, "sidebar_tasks_count", 0)
    # Settings-rail context — derived purely from the nav state already on
    # request.state (no extra DB queries) so the shared settings rail renders
    # with correct role gating on every standalone settings page.
    _nav = ctx.get("nav_projects") or []
    if "active_project_slug" not in ctx:
        _apid = ctx.get("active_project_id")
        ctx["active_project_slug"] = (
            next((p["slug"] for p in _nav if p.get("id") == _apid), _nav[0]["slug"]) if _nav else None
        )
    if "is_install_admin" not in ctx:
        ctx["is_install_admin"] = any(p.get("role") in ("owner", "admin") for p in _nav)
    if "is_superadmin" not in ctx:
        _u = ctx.get("user")
        if isinstance(_u, dict):
            ctx["is_superadmin"] = bool(_u.get("is_superadmin"))
        else:
            ctx["is_superadmin"] = bool(getattr(_u, "is_superadmin", False)) if _u is not None else False
    # Embed/iframe mode has been removed — /settings, /context and /dashboards are
    # now real standalone pages, not iframe hubs. Force embed=False so no page can
    # ever render chromeless (neutralises any stray ?embed=1 link or bookmark).
    ctx["embed"] = False
    return templates.TemplateResponse(request, template_name, ctx, status_code=status_code)


def _base_url_from_request(request: Request) -> str:
    """Derive public base URL from the incoming request (ngrok / localhost / domain)."""
    from app.utils import base_url_from_request

    return base_url_from_request(request)
