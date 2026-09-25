import os
from pathlib import Path

from jinja2 import Environment

from app.templating import templates

TUTORIAL_TEMPLATE = Path("app/templates/tutorial.html")


def test_tutorial_template_syntax_valid():
    source = TUTORIAL_TEMPLATE.read_text()
    Environment().parse(source)


def test_tutorial_template_render_no_project():
    ctx = {
        "user": {
            "id": "123",
            "email": "alex@example.com",
            "display_name": "Alex Carter",
            "is_superadmin": False,
            "permissions": {},
        },
        "has_connections": False,
        "conn_status": {"ga4": False, "gtm": False, "gads": False, "meta": False, "bq": False},
        "conn_live_count": 0,
        "preferred_ai_client": None,
        "active_project_id": None,
        "active_project_name": None,
        "base_url": "https://fluxito.app",
    }
    rendered = templates.get_template("tutorial.html").render(ctx)
    assert "Alex" in rendered
    assert "Alex Carter's Workspace" in rendered


def test_tutorial_template_render_with_project():
    ctx = {
        "user": {
            "id": "456",
            "email": "dev@example.com",
            "display_name": None,
            "is_superadmin": False,
            "permissions": {},
        },
        "has_connections": True,
        "conn_status": {"ga4": True, "gtm": False, "gads": False, "meta": False, "bq": False},
        "conn_live_count": 1,
        "preferred_ai_client": "mcp",
        "active_project_id": "proj-uuid",
        "active_project_name": "Acme Commerce",
        "base_url": "https://fluxito.app",
    }
    rendered = templates.get_template("tutorial.html").render(ctx)
    assert "Dev" in rendered
    assert "Acme Commerce" in rendered


def test_all_app_templates_syntax_valid():
    for root, _, files in os.walk("app/templates"):
        for f in files:
            if f.endswith((".html", ".jinja", ".j2")):
                rel = os.path.relpath(os.path.join(root, f), "app/templates")
                # Ensure the template can be compiled by the app's Jinja environment
                templates.env.get_template(rel)
