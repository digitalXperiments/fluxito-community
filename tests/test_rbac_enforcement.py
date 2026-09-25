# tests/test_rbac_enforcement.py
"""Enforcement-layer tests: choke-point backstop + tool-list filter."""

import pytest

from app.auth.permissions import EffectivePermissions


@pytest.mark.asyncio
async def test_check_tool_permission_denies_ungranted(monkeypatch):
    from app.tools import registry

    eff = EffectivePermissions(full=False, tools={"tagmanager": {"read"}})

    async def fake_resolve(uid, pid):
        return eff

    monkeypatch.setattr(registry, "_resolve_perms_for_call", fake_resolve)

    assert await registry._tool_permitted_for_call("tagmanager_read", {}, "u1", "p1") is True
    assert await registry._tool_permitted_for_call("marketing_read", {}, "u1", "p1") is False
    assert await registry._tool_permitted_for_call("set_active_project", {}, "u1", "p1") is True


@pytest.mark.asyncio
async def test_check_tool_permission_full_allows(monkeypatch):
    from app.tools import registry

    async def fake_resolve(uid, pid):
        return EffectivePermissions(full=True)

    monkeypatch.setattr(registry, "_resolve_perms_for_call", fake_resolve)
    assert await registry._tool_permitted_for_call("marketing_write", {}, "u1", "p1") is True


@pytest.mark.asyncio
async def test_tool_permitted_allows_when_ctx_missing(monkeypatch):
    from app.tools import registry

    # No user/project id → allow (auth handled elsewhere; RBAC only restricts known ctx)
    assert await registry._tool_permitted_for_call("marketing_read", {}, None, None) is True


def test_filter_tool_list_hides_ungranted():
    from app.tools.registry import _filter_tool_names

    names = [
        "analytics_read",
        "analytics_write",
        "marketing_read",
        "set_active_project",
        "get_session_context",
    ]
    eff = EffectivePermissions(full=False, tools={"analytics": {"read"}})
    out = _filter_tool_names(names, eff)
    assert "analytics_read" in out
    assert "set_active_project" in out
    assert "get_session_context" in out
    assert "analytics_write" not in out
    assert "marketing_read" not in out


def test_filter_tool_list_full_keeps_all():
    from app.tools.registry import _filter_tool_names

    names = ["analytics_read", "marketing_write", "run_script"]
    out = _filter_tool_names(names, EffectivePermissions(full=True))
    assert set(out) == set(names)


def test_filter_tool_list_none_eff_keeps_all():
    from app.tools.registry import _filter_tool_names

    names = ["analytics_read", "marketing_write"]
    assert set(_filter_tool_names(names, None)) == set(names)


def test_filter_connections_by_providers():
    from app.auth.mcp_session_manager import _apply_provider_filter

    conns = [
        {"provider": "ga4", "id": "1"},
        {"provider": "amplitude", "id": "2"},
        {"provider": "gtm", "id": "3"},
    ]
    eff = EffectivePermissions(full=False, providers={"ga4", "gtm"})
    kept = {c["provider"] for c in _apply_provider_filter(conns, eff)}
    assert kept == {"ga4", "gtm"}


def test_filter_connections_full_keeps_all():
    from app.auth.mcp_session_manager import _apply_provider_filter

    conns = [{"provider": "amplitude", "id": "2"}]
    assert _apply_provider_filter(conns, EffectivePermissions(full=True)) == conns


def test_filter_connections_none_eff_keeps_all():
    from app.auth.mcp_session_manager import _apply_provider_filter

    conns = [{"provider": "amplitude", "id": "2"}]
    assert _apply_provider_filter(conns, None) == conns
