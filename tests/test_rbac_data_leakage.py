# tests/test_rbac_data_leakage.py
"""End-to-end RBAC data-leakage matrix.

Unlike test_rbac_enforcement.py (which monkeypatches `_resolve_perms_for_call`),
this suite exercises the *real* enforcement stack against a live test DB +
Redis: `resolve_effective_permissions`, the MCP call-time backstop
(`_tool_permitted_for_call`), the per-call project resolver
(`ensure_call_project_ctx`), and `build_project_context`'s membership guard +
provider filter.

Goal: prove no data leakage across roles or projects, and that the two
"fail-permissive" paths (missing ctx → allow; list-filter fail-open) have a
real compensating control behind them.

Requires Postgres (skips otherwise) and uses fakeredis.
"""

import uuid

import pytest

import app.app_state as app_state
from app.auth.mcp_session_manager import (
    _match_membership_project_id,
    build_project_context,
    build_user_context,
    ensure_call_project_ctx,
)
from app.auth.permissions import (
    ALWAYS_ON_TOOLS,
    invalidate_permissions_cache,
    resolve_effective_permissions,
)
from app.tools.registry import _filter_tool_names, _tool_permitted_for_call

# Tools that read/return project data, grouped by the domain each requires.
_READ_TOOLS = [
    "analytics_read",
    "marketing_read",
    "tagmanager_read",
    "seo_read",
    "warehouse_read",
    "automation_read",
]
_WRITE_TOOLS = ["analytics_write", "marketing_write", "tagmanager_write", "seo_write"]


@pytest.fixture
async def wired(db_session_factory, fake_redis):
    """Point app_state at the test DB + fakeredis for the duration of a test."""
    orig_db = app_state.db_session_factory
    orig_redis = app_state.redis_client
    app_state.db_session_factory = db_session_factory
    app_state.redis_client = fake_redis
    try:
        yield
    finally:
        app_state.db_session_factory = orig_db
        app_state.redis_client = orig_redis


async def _seed(db_session_factory):
    """Two projects, each owner+member. Project A has RBAC on with a custom role
    granting ONLY analytics:read + provider ga4, assigned to A's member. A meta
    OAuth connection lives in project A.

    Returns a dict of ids.
    """
    from app.models.connection import OAuthConnection
    from app.models.project import Project, ProjectMember
    from app.models.role import MemberRole, Role
    from app.models.user import User

    async with db_session_factory() as db:
        a_owner = User(email="a-owner@ex.com", display_name="A Owner")
        a_member = User(email="a-member@ex.com", display_name="A Member")
        b_owner = User(email="b-owner@ex.com", display_name="B Owner")
        db.add_all([a_owner, a_member, b_owner])
        await db.flush()

        proj_a = Project(name="Alpha", slug="alpha", owner_id=a_owner.id, rbac_enabled=True)
        proj_b = Project(name="Bravo", slug="bravo", owner_id=b_owner.id, rbac_enabled=True)
        db.add_all([proj_a, proj_b])
        await db.flush()

        pm_a_owner = ProjectMember(project_id=proj_a.id, user_id=a_owner.id, role="owner")
        pm_a_member = ProjectMember(project_id=proj_a.id, user_id=a_member.id, role="member")
        pm_b_owner = ProjectMember(project_id=proj_b.id, user_id=b_owner.id, role="owner")
        db.add_all([pm_a_owner, pm_a_member, pm_b_owner])
        await db.flush()

        # Custom role on A: analytics read + ga4 provider only.
        role = Role(
            project_id=proj_a.id,
            name="Analyst (GA4 read)",
            permissions={"tools": {"analytics": ["read"]}, "providers": ["ga4"]},
            created_by=a_owner.id,
        )
        db.add(role)
        await db.flush()
        db.add(MemberRole(project_member_id=pm_a_member.id, role_id=role.id))

        # A meta connection in project A (token owner = a_owner).
        db.add(
            OAuthConnection(
                project_id=proj_a.id,
                user_id=a_owner.id,
                provider="meta",
                google_email="ads@meta.test",
                access_token_encrypted="x",
                refresh_token_encrypted="x",
            )
        )
        await db.commit()

        return {
            "a_owner": str(a_owner.id),
            "a_member": str(a_member.id),
            "b_owner": str(b_owner.id),
            "proj_a": str(proj_a.id),
            "proj_b": str(proj_b.id),
        }


# ---------------------------------------------------------------------------
# 1. Cross-role within a project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restricted_member_resolves_to_granted_scope_only(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    eff = await resolve_effective_permissions(ids["a_member"], ids["proj_a"])

    assert eff.full is False
    assert eff.tools == {"analytics": {"read"}}
    assert eff.allows_provider("ga4") is True
    assert eff.allows_provider("meta") is False
    assert eff.allows_provider("bigquery") is False


@pytest.mark.asyncio
async def test_backstop_denies_every_ungranted_tool_for_member(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    uid, pid = ids["a_member"], ids["proj_a"]

    # Granted
    assert await _tool_permitted_for_call("analytics_read", {}, uid, pid) is True
    # Denied: other read domains, all writes, advanced tools
    for tool in [
        "marketing_read",
        "tagmanager_read",
        "seo_read",
        "warehouse_read",
        "automation_read",
        "analytics_write",
        "marketing_write",
        "run_script",
        "generic_tool_read",
    ]:
        assert await _tool_permitted_for_call(tool, {}, uid, pid) is False, tool

    # Always-on tools remain callable regardless of role.
    for tool in ALWAYS_ON_TOOLS:
        assert await _tool_permitted_for_call(tool, {}, uid, pid) is True, tool


@pytest.mark.asyncio
async def test_owner_gets_full_access(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    eff = await resolve_effective_permissions(ids["a_owner"], ids["proj_a"])
    assert eff.full is True
    for tool in _READ_TOOLS + _WRITE_TOOLS:
        assert await _tool_permitted_for_call(tool, {}, ids["a_owner"], ids["proj_a"]) is True, tool


@pytest.mark.asyncio
async def test_rbac_disabled_grants_member_full_access(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    from sqlalchemy import update

    from app.models.project import Project

    async with db_session_factory() as db:
        await db.execute(
            update(Project).where(Project.id == uuid.UUID(ids["proj_a"])).values(rbac_enabled=False)
        )
        await db.commit()
    await invalidate_permissions_cache(ids["a_member"], ids["proj_a"])

    eff = await resolve_effective_permissions(ids["a_member"], ids["proj_a"])
    assert eff.full is True


# ---------------------------------------------------------------------------
# 2. Cross-project / cross-tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonmember_resolves_to_deny_all(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    # A's owner has NO membership in project B.
    eff = await resolve_effective_permissions(ids["a_owner"], ids["proj_b"])
    assert eff.full is False
    assert eff.tools == {}
    assert eff.providers == set()
    for tool in _READ_TOOLS:
        assert await _tool_permitted_for_call(tool, {}, ids["a_owner"], ids["proj_b"]) is False, tool


@pytest.mark.asyncio
async def test_build_project_context_403_for_nonmember(wired, db_session_factory):
    from fastapi import HTTPException

    ids = await _seed(db_session_factory)
    with pytest.raises(HTTPException) as exc:
        await build_project_context(ids["proj_b"], ids["a_owner"])
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_explicit_foreign_project_id_does_not_match_membership(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    uctx = await build_user_context(ids["a_member"])
    # member belongs to A only — foreign B id/slug must not match.
    assert _match_membership_project_id(uctx, ids["proj_b"]) is None
    assert _match_membership_project_id(uctx, "bravo") is None
    # its own project still matches
    assert _match_membership_project_id(uctx, ids["proj_a"]) == ids["proj_a"]


@pytest.mark.asyncio
async def test_foreign_project_id_arg_cannot_hijack_active_scope(wired, db_session_factory, fake_redis):
    """A tool call passing project_id=<project B> while the session is scoped to
    A must resolve to A, never B."""
    ids = await _seed(db_session_factory)
    uctx = await build_user_context(ids["a_member"])
    token = app_state.current_user_ctx.set(uctx)
    # session's active project = A
    await fake_redis.set(f"mcp:active_project:{ids['a_member']}", ids["proj_a"])
    try:
        proj_token = await ensure_call_project_ctx("analytics_read", {"project_id": ids["proj_b"]})
        pctx = app_state.current_project_ctx.get()
        assert pctx is not None
        assert pctx.project_id == ids["proj_a"]  # NOT project B
        if proj_token is not None:
            app_state.current_project_ctx.reset(proj_token)
    finally:
        app_state.current_user_ctx.reset(token)
        app_state.current_project_ctx.set(None)


# ---------------------------------------------------------------------------
# 3. Provider filtering (connection-level leakage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_filter_strips_ungranted_connection_for_member(wired, db_session_factory):
    ids = await _seed(db_session_factory)
    # Owner sees the meta connection.
    owner_ctx = await build_project_context(ids["proj_a"], ids["a_owner"])
    assert owner_ctx.has_meta is True
    assert any(c.provider == "meta" for c in owner_ctx.connections)

    # Restricted member (granted ga4 only) must NOT see the meta connection.
    member_ctx = await build_project_context(ids["proj_a"], ids["a_member"])
    assert member_ctx.has_meta is False
    assert not any(c.provider == "meta" for c in member_ctx.connections)


# ---------------------------------------------------------------------------
# 4. Fail-open & missing-ctx compensating controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_project_ctx_resolves_to_no_context(wired, db_session_factory):
    """With a user but no active project (no redis key), ensure_call_project_ctx
    sets NO project context — so tools have no connection to read (the
    compensating control behind 'missing ctx → backstop allows')."""
    ids = await _seed(db_session_factory)
    uctx = await build_user_context(ids["a_member"])
    token = app_state.current_user_ctx.set(uctx)
    app_state.current_project_ctx.set(None)
    try:
        proj_token = await ensure_call_project_ctx("analytics_read", {})
        assert proj_token is None
        assert app_state.current_project_ctx.get() is None
    finally:
        app_state.current_user_ctx.reset(token)
        app_state.current_project_ctx.set(None)


@pytest.mark.asyncio
async def test_hidden_tool_is_also_blocked_at_execution(wired, db_session_factory):
    """Visibility vs execution: a tool filtered OUT of the list for a restricted
    member is ALSO denied by the call-time backstop (defense in depth)."""
    ids = await _seed(db_session_factory)
    uid, pid = ids["a_member"], ids["proj_a"]
    eff = await resolve_effective_permissions(uid, pid)

    visible = set(_filter_tool_names(_READ_TOOLS, eff))
    assert "analytics_read" in visible  # granted → visible
    assert "marketing_read" not in visible  # ungranted → hidden

    # The hidden tool, if called directly anyway, is still blocked.
    assert await _tool_permitted_for_call("marketing_read", {}, uid, pid) is False


# ---------------------------------------------------------------------------
# 5. REGRESSION: cross-tenant warehouse-presence leak via cartesian product
#    in _load_connections_and_resources (previously filtered BQ/Adobe/etc. by an
#    OAuthConnection column). Fixed by scoping each credential query by its own
#    table's column (fluxito_project_id/project_id/user_id).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_LEAK_warehouse_presence_crosses_tenants(wired, db_session_factory):
    """Project A has its own (non-BQ) connection but NO BigQuery connection.
    Project B (different owner, different tenant) has a BigQuery connection.
    A correctly-scoped build_project_context(A) must report has_bq=False.
    """
    from app.models.bq_connection import BQConnection

    ids = await _seed(db_session_factory)

    # Give project B a BigQuery connection owned by B's owner.
    async with db_session_factory() as db:
        db.add(
            BQConnection(
                fluxito_project_id=uuid.UUID(ids["proj_b"]),
                user_id=uuid.UUID(ids["b_owner"]),
                display_name="B's warehouse",
                project_id="gcp-bravo-proj",
                service_account_encrypted="x",
            )
        )
        await db.commit()

    # Build context for project A (which has a meta conn but no BQ of its own).
    a_ctx = await build_project_context(ids["proj_a"], ids["a_owner"])

    # CORRECT behaviour: A must NOT see B's warehouse presence.
    assert a_ctx.has_bq is False, (
        "CROSS-TENANT LEAK: project A reports has_bq=True because "
        "_load_connections_and_resources cartesian-joins bq_connections with "
        "oauth_connections instead of scoping BQ by fluxito_project_id"
    )
