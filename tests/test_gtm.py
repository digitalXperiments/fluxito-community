"""Tests for Google Tag Manager (GTM) setting and template injection."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

import app.app_state as app_state
import app.branding as branding
from app.api.admin_routes import _GTM_RE
from app.settings_service import RUNTIME_SETTING_BY_KEY, get_gtm_container_id
from app.templating import templates


@asynccontextmanager
async def _fake_db_factory():
    yield AsyncMock()


@pytest.fixture(autouse=True)
def _setup_test_env():
    original_cache = branding._GTM_CACHE.get("container_id", "")
    branding._GTM_CACHE["container_id"] = ""
    original_db = app_state.db_session_factory
    app_state.db_session_factory = _fake_db_factory
    yield
    branding._GTM_CACHE["container_id"] = original_cache
    app_state.db_session_factory = original_db


def test_gtm_runtime_setting_registered():
    spec = RUNTIME_SETTING_BY_KEY.get("gtm_container_id")
    assert spec is not None
    assert spec.key == "gtm_container_id"
    assert spec.category == "operations"
    assert spec.env_name == "GTM_CONTAINER_ID"
    assert spec.is_secret is False


def test_gtm_validation_regex():
    valid = [
        "GTM-XXXXXXX",
        "GTM-ABC1234",
        "GTM-5W3K9P",
        "GTM-TEST_1",
        "GTM-A1B2C3D4E5F6G7",
    ]
    for cid in valid:
        assert _GTM_RE.match(cid), f"Expected {cid} to be valid"

    invalid = [
        "",
        "GTM-",
        "gtm-abc",  # must be uppercase
        "<script>alert(1)</script>",
        "GTM-ABC; DROP TABLE",
        "UA-123456-1",
        "GTM-WAY-TOO-LONG-CONTAINER-ID-EXCEEDING-LIMITS",
    ]
    for cid in invalid:
        assert not _GTM_RE.match(cid), f"Expected {cid} to be invalid"


@pytest.mark.asyncio
async def test_gtm_cache_defaults_and_refresh():
    assert branding.gtm_container_id() == ""

    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value="GTM-TEST1234")):
        val = await branding.refresh_gtm()
        assert val == "GTM-TEST1234"
        assert branding.gtm_container_id() == "GTM-TEST1234"

    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value="")):
        val = await branding.refresh_gtm()
        assert val == ""
        assert branding.gtm_container_id() == ""


@pytest.mark.asyncio
async def test_get_gtm_container_id_helper():
    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value="GTM-HELPER1")):
        cid = await get_gtm_container_id()
        assert cid == "GTM-HELPER1"


def test_base_template_without_gtm():
    branding._GTM_CACHE["container_id"] = ""

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "server": ("testserver", 80),
    }
    request = Request(scope)

    html = templates.get_template("base.html").render({"request": request, "user": None, "embed": False})

    assert "googletagmanager.com/gtm.js" not in html
    assert "googletagmanager.com/ns.html" not in html


def test_base_template_with_gtm():
    branding._GTM_CACHE["container_id"] = "GTM-ABC9999"

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "server": ("testserver", 80),
    }
    request = Request(scope)

    html = templates.get_template("base.html").render({"request": request, "user": None, "embed": False})

    # Head script check
    assert "<!-- Google Tag Manager -->" in html
    assert "https://www.googletagmanager.com/gtm.js?id=" in html
    assert "GTM-ABC9999" in html
    assert "<!-- End Google Tag Manager -->" in html

    # Body noscript check
    assert "<!-- Google Tag Manager (noscript) -->" in html
    assert '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-ABC9999"' in html
    assert "<!-- End Google Tag Manager (noscript) -->" in html


def test_admin_template_includes_gtm_controls():
    admin_src = Path("app/templates/admin.html").read_text()

    # Operations panel has the input field
    assert 'id="opGtmId"' in admin_src
    assert "Google Tag Manager (GTM) container ID" in admin_src
    assert "GTM-XXXXXXX" in admin_src

    # JavaScript handles loading and saving
    assert "document.getElementById('opGtmId').value = d.gtm_container_id || '';" in admin_src
    assert "gtm_container_id = document.getElementById('opGtmId').value.trim().toUpperCase();" in admin_src
    assert "gtm_container_id" in admin_src and "JSON.stringify" in admin_src


@pytest.mark.asyncio
async def test_admin_operations_api_gtm_validation():
    from fastapi import HTTPException

    from app.api.admin_routes import admin_set_gtm, admin_set_operations

    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={"gtm_container_id": "<script>alert(1)</script>"})

    with patch("app.api.admin_routes.require_superadmin", new=AsyncMock(return_value={"id": "u1"})):
        with pytest.raises(HTTPException) as exc_op:
            await admin_set_operations(mock_request)
        assert exc_op.value.status_code == 400
        assert "Invalid GTM container ID format" in exc_op.value.detail

        with pytest.raises(HTTPException) as exc_gtm:
            await admin_set_gtm(mock_request)
        assert exc_gtm.value.status_code == 400
        assert "Invalid GTM container ID format" in exc_gtm.value.detail


@pytest.mark.asyncio
async def test_admin_operations_api_gtm_success():
    from app.api.admin_routes import admin_get_operations, admin_set_operations

    mock_req_set = AsyncMock(spec=Request)
    mock_req_set.json = AsyncMock(
        return_value={
            "maintenance_mode": False,
            "announcement_banner": "Test banner",
            "gtm_container_id": "gtm-xyz1234",  # lowercase should be uppercased
        }
    )

    mock_db = AsyncMock()

    @asynccontextmanager
    async def _db_ctx():
        yield mock_db

    with (
        patch(
            "app.api.admin_routes.require_superadmin",
            new=AsyncMock(return_value={"id": "00000000-0000-0000-0000-000000000001"}),
        ),
        patch("app.app_state.db_session_factory", _db_ctx),
        patch("app.settings_service.set_setting", new=AsyncMock()) as mock_set_setting,
        patch("app.branding.refresh_gtm", new=AsyncMock()) as mock_refresh_gtm,
        patch("app.branding.refresh_announcement", new=AsyncMock()),
    ):
        resp = await admin_set_operations(mock_req_set)
        assert resp.status_code == 200
        import json

        body = json.loads(resp.body.decode())
        assert body["gtm_container_id"] == "GTM-XYZ1234"
        mock_refresh_gtm.assert_awaited_once()

    # GET operations returns gtm_container_id
    mock_req_get = AsyncMock(spec=Request)
    with (
        patch(
            "app.api.admin_routes.require_superadmin",
            new=AsyncMock(return_value={"id": "00000000-0000-0000-0000-000000000001"}),
        ),
        patch("app.app_state.db_session_factory", _db_ctx),
        patch(
            "app.settings_service.get_runtime_setting",
            new=AsyncMock(
                side_effect=lambda db, k, default=None: "GTM-XYZ1234" if k == "gtm_container_id" else default
            ),
        ),
    ):
        resp_get = await admin_get_operations(mock_req_get)
        assert resp_get.status_code == 200
        body_get = json.loads(resp_get.body.decode())
        assert body_get["gtm_container_id"] == "GTM-XYZ1234"
