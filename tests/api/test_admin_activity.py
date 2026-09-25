"""Tests for Super Admin Platform Activity Log, Projects Directory, and Navigation."""

import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.app_state as app_state
from app.models.audit import ToolCallAudit
from app.models.project import Project, ProjectMember
from app.models.user import User


def test_base_sidebar_has_superadmin_gated_admin_panel():
    base_html = Path("app/templates/base.html").read_text()
    assert "{% if is_superadmin %}" in base_html
    assert 'href="/admin"' in base_html
    assert "Admin Panel" in base_html
    assert "sidebar-badge" in base_html


def test_settings_rail_does_not_have_platform_or_admin():
    """Settings rail must NOT contain platform or admin items (Option B strict separation)."""
    rail_html = Path("app/templates/partials/settings_rail.html").read_text()
    assert 'href="/admin/activity"' not in rail_html
    assert 'href="/admin/projects"' not in rail_html
    assert 'href="/admin"' not in rail_html
    assert "Platform Activity" not in rail_html
    assert "Platform Settings" not in rail_html


def test_admin_rail_has_platform_activity_and_projects():
    """Admin rail has dedicated navigation for activity, projects, and settings."""
    rail_html = Path("app/templates/partials/admin_rail.html").read_text()
    assert 'href="/admin/activity"' in rail_html
    assert "Platform Activity" in rail_html
    assert 'href="/admin/projects"' in rail_html
    assert "Projects Directory" in rail_html
    assert 'href="/admin"' in rail_html
    assert "Platform Settings" in rail_html


def test_admin_shell_and_templates_structure():
    """Admin shell extends base.html and admin templates extend admin/shell.html."""
    shell_html = Path("app/templates/admin/shell.html").read_text()
    assert '{% extends "base.html" %}' in shell_html
    assert 'class="app-main settings-page admin-page"' in shell_html
    assert "partials/admin_rail.html" in shell_html
    assert "admin_main" in shell_html

    act_html = Path("app/templates/admin_activity.html").read_text()
    assert '{% extends "admin/shell.html" %}' in act_html
    assert "admin_section = 'activity'" in act_html
    assert "admin_main" in act_html

    proj_html = Path("app/templates/admin_projects.html").read_text()
    assert '{% extends "admin/shell.html" %}' in proj_html
    assert "admin_section = 'projects'" in proj_html
    assert "admin_main" in proj_html

    admin_html = Path("app/templates/admin.html").read_text()
    assert '{% extends "admin/shell.html" %}' in admin_html
    assert "admin_section = 'settings'" in admin_html
    assert "admin_main" in admin_html


def test_admin_activity_template_elements():
    act_html = Path("app/templates/admin_activity.html").read_text()
    assert "Platform activity" in act_html
    assert 'name="user_id"' in act_html
    assert 'name="project_id"' in act_html
    assert 'name="platform"' in act_html
    assert 'name="tool"' in act_html
    assert 'name="status"' in act_html
    assert 'name="start_date"' in act_html
    assert 'name="end_date"' in act_html
    assert 'id="dateRangePicker"' in act_html
    assert 'id="dateRangePopover"' in act_html
    assert 'data-searchable="true"' in act_html
    assert "/admin/activity/" in act_html


def test_admin_activity_script_syntax():
    import re
    import shutil
    import subprocess

    act_html = Path("app/templates/admin_activity.html").read_text()
    match = re.search(r"<script>(.*?)</script>", act_html, re.DOTALL)
    assert match is not None
    script = match.group(1)
    assert "initSearchableSelects" in script
    assert "toggleDateRangePopover" in script
    assert "selectDatePreset" in script
    assert "applyCustomDateRange" in script

    node_bin = shutil.which("node")
    if node_bin:
        res = subprocess.run(
            [node_bin, "-e", f"new Function({script!r})"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"Node syntax error: {res.stderr}"


def test_admin_projects_template_elements():
    proj_html = Path("app/templates/admin_projects.html").read_text()
    assert "Instance projects" in proj_html
    assert "projectsTable" in proj_html
    assert "/admin/activity?project_id=" in proj_html


@pytest.fixture
async def admin_test_context(db_engine, db_session_factory, monkeypatch):
    monkeypatch.setattr(app_state, "db_session_factory", db_session_factory)

    async with db_session_factory() as s:
        # 1. Super admin user
        super_user = User(
            email=f"super-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Super Admin User",
            is_superadmin=True,
        )
        s.add(super_user)

        # 2. Regular user
        reg_user = User(
            email=f"reg-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Regular User",
            is_superadmin=False,
        )
        s.add(reg_user)
        await s.flush()

        # 3. Project
        project = Project(
            name="Alpha Corp",
            slug=f"alpha-{uuid.uuid4().hex[:8]}",
            owner_id=reg_user.id,
        )
        s.add(project)
        await s.flush()

        # 4. Project Member
        s.add(
            ProjectMember(
                project_id=project.id,
                user_id=reg_user.id,
                role="owner",
                is_active=True,
            )
        )

        # 5. Tool call audits
        call1 = ToolCallAudit(
            user_id=reg_user.id,
            project_id=project.id,
            tool_name="analytics_run_report",
            platform="ga4",
            source_client="claude",
            status="success",
            is_write=False,
            duration_ms=320,
            response_summary="Returned 10 rows",
            arguments={"property_id": "12345"},
        )
        call2 = ToolCallAudit(
            user_id=super_user.id,
            project_id=project.id,
            tool_name="tagmanager_publish_container",
            platform="gtm",
            source_client="cursor",
            status="success",
            is_write=True,
            duration_ms=650,
            response_summary="Published v12",
            arguments={"container_id": "GTM-XYZ"},
        )
        s.add_all([call1, call2])
        await s.commit()

        super_uid = str(super_user.id)
        reg_uid = str(reg_user.id)
        pid = str(project.id)

    return {
        "super_uid": super_uid,
        "reg_uid": reg_uid,
        "pid": pid,
    }


@pytest.mark.asyncio
async def test_admin_activity_gating_unauthenticated():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/admin/activity", follow_redirects=False)
        assert res.status_code == 302
        assert "/signin" in res.headers.get("location", "")


@pytest.mark.asyncio
async def test_admin_activity_gating_regular_user(admin_test_context, monkeypatch):
    import app.api.admin_routes as ar

    async def fake_resolve_ctx(request):
        from types import SimpleNamespace

        return SimpleNamespace(user_id=admin_test_context["reg_uid"], email="reg@example.com")

    monkeypatch.setattr(ar, "_resolve_user_ctx", fake_resolve_ctx)

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/admin/activity", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers.get("location") == "/home"

        # API endpoint returns 403
        api_res = await c.get("/api/admin/activity")
        assert api_res.status_code == 403


@pytest.mark.asyncio
async def test_admin_activity_superadmin_access(admin_test_context, monkeypatch):
    import app.api.admin_routes as ar

    async def fake_resolve_ctx(request):
        from types import SimpleNamespace

        return SimpleNamespace(user_id=admin_test_context["super_uid"], email="super@example.com")

    monkeypatch.setattr(ar, "_resolve_user_ctx", fake_resolve_ctx)

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # 1. HTML Page
        res = await c.get("/admin/activity")
        assert res.status_code == 200
        assert "Platform activity" in res.text
        assert "analytics_run_report" in res.text

        # 2. JSON API
        api_res = await c.get("/api/admin/activity")
        assert api_res.status_code == 200
        data = api_res.json()
        assert "calls" in data
        assert len(data["calls"]) >= 2

        # 3. Filter by user_id
        filtered_res = await c.get(f"/api/admin/activity?user_id={admin_test_context['reg_uid']}")
        assert filtered_res.status_code == 200
        f_data = filtered_res.json()
        assert all(c["user_id"] == admin_test_context["reg_uid"] for c in f_data["calls"])

        # 4. Filter by writes only
        writes_res = await c.get("/api/admin/activity?status=write")
        assert writes_res.status_code == 200
        w_data = writes_res.json()
        assert all(c["is_write"] is True for c in w_data["calls"])

        # 5. CSV format export
        csv_res = await c.get("/api/admin/activity?format=csv")
        assert csv_res.status_code == 200
        assert "text/csv" in csv_res.headers.get("content-type", "")
        assert "tool_name" in csv_res.text


@pytest.mark.asyncio
async def test_admin_projects_superadmin_access(admin_test_context, monkeypatch):
    import app.api.admin_routes as ar

    async def fake_resolve_ctx(request):
        from types import SimpleNamespace

        return SimpleNamespace(user_id=admin_test_context["super_uid"], email="super@example.com")

    monkeypatch.setattr(ar, "_resolve_user_ctx", fake_resolve_ctx)

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # 1. HTML Page
        res = await c.get("/admin/projects")
        assert res.status_code == 200
        assert "Alpha Corp" in res.text

        # 2. JSON API
        api_res = await c.get("/api/admin/projects")
        assert api_res.status_code == 200
        data = api_res.json()
        assert "projects" in data
        assert any(p["name"] == "Alpha Corp" for p in data["projects"])


@pytest.mark.asyncio
async def test_admin_api_activity_and_csv_mocked(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import app.api.admin_routes as ar

    async def fake_require_superadmin(request):
        return {"id": str(uuid.uuid4()), "email": "super@example.com", "is_superadmin": True}

    monkeypatch.setattr(ar, "require_superadmin", fake_require_superadmin)

    mock_audit = MagicMock()
    mock_audit.id = uuid.uuid4()
    mock_audit.created_at = None
    mock_audit.tool_name = "analytics_run_report"
    mock_audit.platform = "ga4"
    mock_audit.source_client = "claude"
    mock_audit.status = "success"
    mock_audit.is_write = False
    mock_audit.duration_ms = 120
    mock_audit.response_summary = "10 rows"
    mock_audit.to_dict.return_value = {
        "id": str(mock_audit.id),
        "tool_name": mock_audit.tool_name,
        "platform": mock_audit.platform,
        "source_client": mock_audit.source_client,
        "status": mock_audit.status,
        "is_write": False,
        "duration_ms": 120,
        "response_summary": "10 rows",
    }

    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.all.return_value = [(mock_audit, "user@example.com", "User Name", "Alpha Project")]
    mock_db.execute.return_value = mock_res

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(app_state, "db_session_factory", MockSessionFactory())

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/api/admin/activity")
        assert res.status_code == 200
        data = res.json()
        assert "calls" in data
        assert len(data["calls"]) == 1
        assert data["calls"][0]["user_email"] == "user@example.com"
        assert data["calls"][0]["project_name"] == "Alpha Project"

        csv_res = await c.get("/api/admin/activity?format=csv")
        assert csv_res.status_code == 200
        assert "text/csv" in csv_res.headers.get("content-type", "")
        assert "user@example.com" in csv_res.text

        # Test date range params
        range_res = await c.get("/api/admin/activity?start_date=2026-09-01&end_date=2026-09-15&days=7")
        assert range_res.status_code == 200


@pytest.mark.asyncio
async def test_admin_activity_page_html_mocked(monkeypatch):
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    import app.api.admin_routes as ar

    async def fake_resolve_user_ctx(request):
        from types import SimpleNamespace

        return SimpleNamespace(user_id=str(uuid.uuid4()), email="super@example.com")

    monkeypatch.setattr(ar, "_resolve_user_ctx", fake_resolve_user_ctx)

    super_user = User(
        id=uuid.uuid4(),
        email="super@example.com",
        display_name="Super Admin",
        is_superadmin=True,
    )

    mock_audit = MagicMock()
    mock_audit.id = uuid.uuid4()
    mock_audit.created_at = datetime.utcnow()
    mock_audit.tool_name = "analytics_run_report"
    mock_audit.platform = "ga4"
    mock_audit.source_client = "claude"
    mock_audit.status = "success"
    mock_audit.is_write = False
    mock_audit.duration_ms = 120
    mock_audit.response_summary = "10 rows"
    mock_audit.user_id = super_user.id
    mock_audit.project_id = uuid.uuid4()

    mock_db = AsyncMock()

    async def fake_execute(stmt):
        mock_result = MagicMock()
        s_str = str(stmt)
        if "FROM users" in s_str:
            mock_result.scalar_one_or_none.return_value = super_user
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = [super_user]
            mock_result.scalars.return_value = mock_scalars
        elif "FROM projects" in s_str:
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = []
            mock_result.scalars.return_value = mock_scalars
        elif "DISTINCT tool_call_audit.tool_name" in s_str:
            mock_result.all.return_value = [("analytics_run_report",)]
        elif "DISTINCT tool_call_audit.platform" in s_str:
            mock_result.all.return_value = [("ga4",)]
        else:
            mock_result.all.return_value = [
                (mock_audit, "super@example.com", "Super Admin", "Alpha Project", "alpha-project")
            ]
        return mock_result

    mock_db.execute = fake_execute

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(app_state, "db_session_factory", MockSessionFactory())

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/admin/activity")
        assert res.status_code == 200
        assert "Platform activity" in res.text
        assert "analytics_run_report" in res.text
        assert "Platform Activity" in res.text
        assert "admin-rail" in res.text
        assert "admin-shell" in res.text

        # Test custom date range query params
        res_custom = await c.get("/admin/activity?start_date=2026-09-01&end_date=2026-09-15")
        assert res_custom.status_code == 200
        assert "2026-09-01" in res_custom.text
        assert "2026-09-15" in res_custom.text


@pytest.mark.asyncio
async def test_admin_projects_page_html_mocked(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import app.api.admin_routes as ar

    async def fake_resolve_user_ctx(request):
        from types import SimpleNamespace

        return SimpleNamespace(user_id=str(uuid.uuid4()), email="super@example.com")

    monkeypatch.setattr(ar, "_resolve_user_ctx", fake_resolve_user_ctx)

    super_user = User(
        id=uuid.uuid4(),
        email="super@example.com",
        display_name="Super Admin",
        is_superadmin=True,
    )

    proj = Project(
        id=uuid.uuid4(),
        name="Mock Project",
        slug="mock-project",
        is_active=True,
        owner_id=super_user.id,
    )

    mock_db = AsyncMock()

    async def fake_execute(stmt):
        mock_result = MagicMock()
        s_str = str(stmt)
        if "WHERE users.id =" in s_str:
            mock_result.scalar_one_or_none.return_value = super_user
        elif "FROM projects" in s_str:
            mock_result.all.return_value = [(proj, "super@example.com", "Super Admin")]
        else:
            mock_result.all.return_value = []
        return mock_result

    mock_db.execute = fake_execute

    class MockSessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(app_state, "db_session_factory", MockSessionFactory())

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/admin/projects")
        assert res.status_code == 200
        assert "Instance projects" in res.text
        assert "Mock Project" in res.text
        assert "admin-rail" in res.text
        assert "admin-shell" in res.text
