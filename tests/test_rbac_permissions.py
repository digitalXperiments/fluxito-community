# tests/test_rbac_permissions.py
"""Unit tests for the RBAC permission vocabulary and resolver."""

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


def test_domain_tools_map_covers_dispatchers():
    from app.auth.permissions import DOMAIN_TOOLS

    assert DOMAIN_TOOLS["tagmanager"]["read"] == {"tagmanager_read"}
    assert "tagmanager_write" in DOMAIN_TOOLS["tagmanager"]["write"]
    assert "analytics_read" in DOMAIN_TOOLS["analytics"]["read"]
    assert DOMAIN_TOOLS["audience"]["read"] == {"audience_read"}
    assert DOMAIN_TOOLS["audience"]["write"] == {"audience_write"}


def test_always_on_tools():
    from app.auth.permissions import ALWAYS_ON_TOOLS

    assert {"get_session_context", "list_my_projects", "set_active_project"} <= ALWAYS_ON_TOOLS


def test_full_permissions_allow_everything():
    from app.auth.permissions import EffectivePermissions

    eff = EffectivePermissions(full=True)
    assert eff.allows_tool("marketing_write") is True
    assert eff.allows_provider("amplitude") is True
    assert eff.allows_provider("mixpanel") is True
    assert eff.allows_provider("posthog") is True


def test_scoped_permissions_allow_only_granted():
    from app.auth.permissions import EffectivePermissions

    eff = EffectivePermissions(
        full=False,
        tools={"tagmanager": {"read", "write"}, "analytics": {"read"}},
        providers={"ga4", "gtm"},
    )
    assert eff.allows_tool("tagmanager_write") is True
    assert eff.allows_tool("analytics_read") is True
    assert eff.allows_tool("analytics_write") is False
    assert eff.allows_tool("marketing_read") is False
    assert eff.allows_tool("set_active_project") is True
    assert eff.allows_provider("ga4") is True
    assert eff.allows_provider("amplitude") is False
    assert eff.allows_provider("mixpanel") is False
    assert eff.allows_provider("posthog") is False


def test_analysis_tools_are_in_permission_map():
    # FINDINGS S1 #10: these were absent, so non-full users were denied the direct
    # tools while the run_audit twins stayed open.
    from app.auth.permissions import _TOOL_TO_REQ

    for t in ("tag_rulebook", "live_tag_test", "save_audit_result"):
        assert t in _TOOL_TO_REQ, f"{t} missing from the permission map"


def test_analysis_read_allows_reads_not_writes():
    # FINDINGS S1 #9 + #10: analysis:read covers the direct tools and run_audit
    # READ actions, but mutations require analysis:write — even via run_audit.
    from app.auth.permissions import EffectivePermissions

    eff = EffectivePermissions(full=False, tools={"analysis": {"read"}})
    # reads
    assert eff.allows_tool("run_audit", action="gtm_audit_container") is True
    assert eff.allows_tool("tag_rulebook", action="validate_payload") is True
    assert eff.allows_tool("save_audit_result", action="list_runs") is True
    assert eff.allows_tool("live_tag_test", action="get_test_plan") is True
    # writes — denied under read-only
    assert eff.allows_tool("save_audit_result", action="save") is False
    assert eff.allows_tool("tag_rulebook", action="delete_custom_rule") is False
    assert eff.allows_tool("run_audit", action="save_audit_result") is False
    assert eff.allows_tool("run_audit", action="tag_delete_custom_rule") is False


def test_analysis_write_allows_mutations():
    from app.auth.permissions import EffectivePermissions

    eff = EffectivePermissions(full=False, tools={"analysis": {"read", "write"}})
    assert eff.allows_tool("save_audit_result", action="save") is True
    assert eff.allows_tool("tag_rulebook", action="save_custom_rule") is True
    assert eff.allows_tool("run_audit", action="save_audit_result") is True
    assert eff.allows_tool("run_audit", action="live_tag_finish_session") is True


def test_no_analysis_grant_denies_everything():
    from app.auth.permissions import EffectivePermissions

    eff = EffectivePermissions(full=False, tools={})
    assert eff.allows_tool("tag_rulebook", action="validate_payload") is False
    assert eff.allows_tool("run_audit", action="gtm_audit_container") is False


def test_scripting_is_advanced_gate():
    from app.auth.permissions import EffectivePermissions

    denied = EffectivePermissions(full=False, advanced=set())
    assert denied.allows_tool("run_script") is False
    granted = EffectivePermissions(full=False, advanced={"scripting"})
    assert granted.allows_tool("run_script") is True


def test_normalize_permissions_write_implies_read():
    from app.auth.permissions import normalize_permissions

    out = normalize_permissions({"tools": {"tagmanager": ["write"]}, "providers": ["ga4"]})
    assert set(out["tools"]["tagmanager"]) == {"read", "write"}
    assert out["providers"] == ["ga4"]


def test_normalize_rejects_unknown_domain_and_provider():
    from app.auth.permissions import PermissionValidationError, normalize_permissions

    with pytest.raises(PermissionValidationError):
        normalize_permissions({"tools": {"nope": ["read"]}})
    with pytest.raises(PermissionValidationError):
        normalize_permissions({"providers": ["myspace"]})


def test_mixpanel_posthog_in_providers_list():
    from app.auth.permissions import PROVIDERS

    assert "mixpanel" in PROVIDERS
    assert "posthog" in PROVIDERS
    assert "data_manager" in PROVIDERS


def test_union_roles_builds_effective():
    from app.auth.permissions import _union_role_docs

    eff = _union_role_docs(
        [
            {"tools": {"seo": ["read"]}, "providers": ["gsc"]},
            {
                "tools": {"analytics": ["read", "write"]},
                "providers": ["ga4"],
                "advanced": {"scripting": True},
            },
        ]
    )
    assert eff.full is False
    assert eff.tools["seo"] == {"read"}
    assert eff.tools["analytics"] == {"read", "write"}
    assert eff.providers == {"gsc", "ga4"}
    assert "scripting" in eff.advanced


@pytest.mark.asyncio
async def test_resolver_owner_is_full(_patch_db, db_session_factory, monkeypatch):
    from unittest.mock import AsyncMock

    import app.auth.permissions as perms
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="own@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="rp1", owner_id=u.id, rbac_enabled=True)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
        await db.flush()
        uid, pid = str(u.id), str(p.id)
        await db.commit()

    monkeypatch.setattr(perms, "_cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(perms, "_cache_set", AsyncMock(return_value=None))
    eff = await perms.resolve_effective_permissions(uid, pid)
    assert eff.full is True


@pytest.mark.asyncio
async def test_resolver_member_rbac_off_is_full(_patch_db, db_session_factory, monkeypatch):
    from unittest.mock import AsyncMock

    import app.auth.permissions as perms
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="mem@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="rp2", owner_id=u.id, rbac_enabled=False)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=u.id, role="member"))
        await db.flush()
        uid, pid = str(u.id), str(p.id)
        await db.commit()

    monkeypatch.setattr(perms, "_cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(perms, "_cache_set", AsyncMock(return_value=None))
    eff = await perms.resolve_effective_permissions(uid, pid)
    assert eff.full is True


@pytest.mark.asyncio
async def test_resolver_member_unions_assigned_roles(_patch_db, db_session_factory, monkeypatch):
    from unittest.mock import AsyncMock

    import app.auth.permissions as perms
    from app.models.project import Project, ProjectMember
    from app.models.role import MemberRole, Role
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="scoped@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="rp3", owner_id=u.id, rbac_enabled=True)
        db.add(p)
        await db.flush()
        pm = ProjectMember(project_id=p.id, user_id=u.id, role="member")
        db.add(pm)
        await db.flush()
        r1 = Role(
            project_id=p.id,
            name="SEO",
            permissions={"tools": {"seo": ["read"]}, "providers": ["gsc"]},
            created_by=u.id,
        )
        r2 = Role(
            project_id=p.id,
            name="View",
            permissions={"tools": {"analytics": ["read"]}, "providers": ["ga4"]},
            created_by=u.id,
        )
        db.add_all([r1, r2])
        await db.flush()
        db.add_all(
            [
                MemberRole(project_member_id=pm.id, role_id=r1.id),
                MemberRole(project_member_id=pm.id, role_id=r2.id),
            ]
        )
        await db.flush()
        uid, pid = str(u.id), str(p.id)
        await db.commit()

    monkeypatch.setattr(perms, "_cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(perms, "_cache_set", AsyncMock(return_value=None))
    eff = await perms.resolve_effective_permissions(uid, pid)
    assert eff.full is False
    assert eff.tools["seo"] == {"read"}
    assert eff.tools["analytics"] == {"read"}
    assert eff.providers == {"gsc", "ga4"}
