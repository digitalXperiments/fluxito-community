"""The chat page and the AI settings page render, with no Flux branding."""

import pytest
from starlette.requests import Request

from app.templating import templates

_USER = {"id": "uid", "email": "t@example.com", "display_name": "T", "permissions": {}}
_PRESETS = [
    {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
]


def _render(name, **ctx):
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    base = {"request": req, "user": _USER, "embed": False, "perms": {"full": True, "tools": {}}}
    return templates.get_template(name).render({**base, **ctx})


@pytest.mark.parametrize("configured", [True, False])
def test_chat_page_renders(configured):
    html = _render("chat.html", chat_configured=configured, active="chat")
    assert 'id="chat-composer"' in html and "/static/js/chat.js" in html
    assert ('href="/settings/ai">Add your model' in html) is not configured
    assert "flux" not in html.lower().replace("fluxito", "")


def test_ai_settings_renders_presets_and_masks_key():
    cfg = {"base_url": "https://openrouter.ai/api/v1", "model": "m", "has_key": True, "key_hint": "••••abcd"}
    html = _render("settings/ai.html", chat_config=cfg, presets=_PRESETS)
    assert 'data-base-url="https://api.openai.com/v1"' in html
    assert 'data-base-url="https://openrouter.ai/api/v1"' in html
    assert 'id="ai-test"' in html and 'id="ai-remove"' in html
    assert 'type="password"' in html and 'value=""' in html
    assert "flux" not in html.lower().replace("fluxito", "")


def test_ai_settings_renders_unconfigured():
    html = _render("settings/ai.html", chat_config=None, presets=_PRESETS)
    assert "Not set up" in html and 'id="ai-remove"' not in html
