"""Legacy /settings entry point.

Settings used to be a single iframe shell with embedded panels. They are now
real standalone pages (each with the shared settings rail). This route only
preserves old links/bookmarks: it redirects ``/settings`` (and the legacy
``?tab=`` deep-links) to the matching real page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.auth.uid_cookie import get_uid_from_request

router = APIRouter()

# Legacy ?tab= value -> real page route.
_TAB_DESTINATIONS = {
    "account": "/profile",
    "ai": "/settings/ai",
    "integrations": "/settings/connections",
    "connections": "/settings/connections",
    "connect": "/settings/connections",
    "system": "/admin",  # System settings retired — superadmin console covers it
    "activity": "/activity-log",
    "platform": "/admin",
    "platform_activity": "/admin/activity",
    "platform_projects": "/admin/projects",
}


@router.get("/settings")
async def settings_page(request: Request):
    """Redirect to the real settings page matching the legacy ?tab= value."""
    uid = get_uid_from_request(request)
    if not uid:
        return RedirectResponse("/signin?next=/settings", status_code=302)

    tab = request.query_params.get("tab")
    from app.branding import chat_enabled

    if tab == "ai" and not chat_enabled():
        return RedirectResponse("/settings", status_code=302)

    # Bare /settings (no legacy ?tab=) opens Settings on the Profile page,
    # which carries the settings rail. Connections has its own nav entry.
    if not tab:
        return RedirectResponse("/profile", status_code=302)

    if tab == "project":
        # Resolve the active project slug from nav state (set by middleware).
        slug = None
        state = getattr(request, "state", None)
        if state is not None:
            nav = getattr(state, "nav_projects", []) or []
            apid = getattr(state, "active_project_id", None)
            slug = next(
                (p["slug"] for p in nav if p["id"] == apid),
                (nav[0]["slug"] if nav else None),
            )
        return RedirectResponse(f"/project/{slug}/settings" if slug else "/profile", status_code=302)

    return RedirectResponse(_TAB_DESTINATIONS.get(tab, "/profile"), status_code=302)
