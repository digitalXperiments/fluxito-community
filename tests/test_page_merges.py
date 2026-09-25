"""File-read tests for the Context page merge.

These pages used to embed their sub-pages in ``?embed=1`` iframes. They are now
real standalone pages joined by a shared sub-nav tab bar
(partials/context_tabs.html). Pure file reads.
"""

from pathlib import Path

CONTEXT_TABS = Path("app/templates/partials/context_tabs.html")
KPI = Path("app/templates/kpi_library.html")
BUSINESS = Path("app/templates/business_context.html")
KNOWLEDGE_ROUTES = Path("app/api/knowledge_routes.py")


# ---------------------------------------------------------------------------
# Old iframe hubs are gone
# ---------------------------------------------------------------------------


def test_iframe_hubs_removed():
    assert not Path("app/templates/context.html").exists()
    assert not Path("app/templates/dashboards_hub.html").exists()


# ---------------------------------------------------------------------------
# Context — sub-nav tabs are real links
# ---------------------------------------------------------------------------


def test_context_tabs_are_real_links():
    src = CONTEXT_TABS.read_text()
    assert 'href="/kpi-library"' in src
    assert 'href="/business-context"' in src
    assert "context_section ==" in src


def test_context_pages_include_tabs_and_section():
    kpi = KPI.read_text()
    assert 'include "partials/context_tabs.html"' in kpi
    assert "context_section = 'kpi'" in kpi
    biz = BUSINESS.read_text()
    assert 'include "partials/context_tabs.html"' in biz
    assert "context_section = 'business'" in biz


def test_context_route_redirects_to_kpi_library():
    src = KNOWLEDGE_ROUTES.read_text()
    assert 'RedirectResponse("/kpi-library"' in src


def test_context_sub_routes_no_longer_embed_guarded():
    src = KNOWLEDGE_ROUTES.read_text()
    assert 'request.query_params.get("embed")' not in src
