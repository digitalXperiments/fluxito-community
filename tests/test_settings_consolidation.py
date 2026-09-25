"""File-read tests for the real-page settings system.

Settings used to be a single iframe shell (settings/index.html) with embedded
``?embed=1`` panels. They are now real standalone pages joined by a shared
settings rail (partials/settings_rail.html + settings/shell.html). These pure
file-read tests lock in the new structure.
"""

from pathlib import Path

RAIL = Path("app/templates/partials/settings_rail.html")
SHELL = Path("app/templates/settings/shell.html")
SETTINGS_ROUTES = Path("app/api/settings_routes.py")
TEMPLATING = Path("app/templating.py")
TEMPLATES_DIR = Path("app/templates")

SETTINGS_PAGES = {
    "profile.html": "account",
    "settings/ai.html": "ai",
    "connect.html": "connections",
    "audit.html": "activity",
    "projects/settings.html": "project",
}


# ---------------------------------------------------------------------------
# The old iframe shell is gone
# ---------------------------------------------------------------------------


def test_iframe_settings_hub_removed():
    assert not Path("app/templates/settings/index.html").exists()


def test_no_embed_panels_anywhere():
    for tpl in TEMPLATES_DIR.rglob("*.html"):
        src = tpl.read_text()
        assert "?embed=1" not in src, f"{tpl} still has an ?embed=1 link"
        assert "settings-frame" not in src, f"{tpl} still references the iframe"
        assert "data-src=" not in src, f"{tpl} still has an iframe data-src"


# ---------------------------------------------------------------------------
# Shared rail — real links, role gated
# ---------------------------------------------------------------------------


def test_rail_has_real_anchor_links():
    src = RAIL.read_text()
    for href in (
        "/profile",
        "/settings/ai",
    ):
        assert f'href="{href}"' in src, f"rail missing real link {href}"
    assert 'href="/project/{{ active_project_slug }}/settings"' in src
    # Admin Panel is decoupled to partials/admin_rail.html (Option B)
    assert 'href="/admin"' not in src


def test_rail_role_gates():
    src = RAIL.read_text()
    assert "{% if active_project_slug %}" in src
    # OAuth apps are instance-wide: they live in the Admin rail, not Settings.
    assert "/settings/integrations" not in src
    assert 'href="/admin/oauth-apps"' in Path("app/templates/partials/admin_rail.html").read_text()
    # Platform / superadmin section is decoupled into partials/admin_rail.html (Option B)
    assert "{% if is_superadmin %}" not in src


def test_rail_highlights_active_section():
    src = RAIL.read_text()
    assert "settings_section ==" in src
    assert "is-active" in src
    # In-page tabs are highlighted from the hash / the page's settings:tab event.
    assert "settings:tab" in src
    assert "hashchange" in src


def test_rail_matches_prototype_groups():
    src = RAIL.read_text()
    for label in (
        "Profile",
        "Preferences",
        "Usage",
        "General",
        "Members &amp; roles",
        "Notification channels",
        "API limits",
    ):
        assert f">{label}" in src, label
    # Connections, AI & MCP and Activity log are top-level sidebar items —
    # not repeated in the rail. "OAuth apps" here is the project's own apps
    # (owner/admin); the install-wide ones live in the Admin panel.
    for label in ("Connections", "AI &amp; MCP", "Activity log"):
        assert f">{label}" not in src, label
    assert 'settings#oauth" class="settings-tab" data-rail-section="project" data-rail-hash="oauth"' in src
    assert 'href="/profile#activity"' in src
    # On a project's own settings page the rail follows that project.
    assert "{% set active_project_slug = project.slug %}" in src


# ---------------------------------------------------------------------------
# Shell + child pages
# ---------------------------------------------------------------------------


def test_shell_extends_base_and_includes_rail():
    src = SHELL.read_text()
    assert '{% extends "base.html" %}' in src
    assert 'include "partials/settings_rail.html"' in src
    assert "{% block settings_main %}" in src
    assert "settings-shell" in src


def test_settings_pages_use_the_shell():
    for page, section in SETTINGS_PAGES.items():
        src = (TEMPLATES_DIR / page).read_text()
        assert '{% extends "settings/shell.html" %}' in src, f"{page} not on the shell"
        assert f"settings_section = '{section}'" in src, f"{page} wrong/no section"
        assert "{% block settings_main %}" in src, f"{page} missing settings_main block"


# ---------------------------------------------------------------------------
# /settings is now a redirect, not a rendered shell
# ---------------------------------------------------------------------------


def test_settings_route_redirects():
    src = SETTINGS_ROUTES.read_text()
    assert "RedirectResponse" in src
    assert '"/profile"' in src
    assert "settings/index.html" not in src
    assert '"/activity-log"' in src and '"/admin"' in src


# ---------------------------------------------------------------------------
# render() injects rail context + retires embed
# ---------------------------------------------------------------------------


def test_render_injects_rail_flags():
    src = TEMPLATING.read_text()
    assert "is_install_admin" in src
    assert "is_superadmin" in src
    assert "active_project_slug" in src


def test_render_forces_embed_false():
    src = TEMPLATING.read_text()
    assert 'ctx["embed"] = False' in src
