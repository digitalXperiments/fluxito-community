"""Tests for Adobe Launch list_companies and list_properties on tagmanager_read."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

import app.app_state as state
from app.auth.mcp_session_manager import UserContext
from app.connectors.adobe_launch import AdobeLaunchConnector
from app.tools import specs
from app.tools.registry import register_all_tools
from app.tools.unified import TAGMANAGER_READ_ROUTES


@pytest.fixture
def mcp_server():
    mcp = FastMCP(name="test-fluxito-launch")
    register_all_tools(mcp)
    return mcp


def test_list_companies_in_specs():
    tagmanager_specs = specs.specs_for("tagmanager_read")
    list_companies_spec = next((s for s in tagmanager_specs if s.action == "list_companies"), None)
    assert list_companies_spec is not None, "list_companies spec is missing from tagmanager_read"
    assert list_companies_spec.group == "ADOBE LAUNCH"
    assert "adobe_launch" in list_companies_spec.platforms


def test_list_companies_in_routes():
    assert "list_companies" in TAGMANAGER_READ_ROUTES
    route = TAGMANAGER_READ_ROUTES["list_companies"]
    assert route[0] == "tagmanager_read"
    assert route[1] == "list_companies"
    assert route[2] == {"platform": "adobe_launch"}


@pytest.mark.asyncio
async def test_tagmanager_read_describe_list_companies(mcp_server):
    tm = mcp_server._tool_manager
    tagmanager_read = tm._tools["tagmanager_read"].fn

    res = await tagmanager_read(action="describe", params={"action": "list_companies"})
    assert res.get("action") == "list_companies"
    assert res.get("tool") == "tagmanager_read"
    assert "adobe_launch" in res.get("spec", {}).get("platforms", [])
    assert "companies" in res.get("spec", {}).get("summary", "").lower()


@pytest.mark.asyncio
async def test_tagmanager_read_list_companies_dispatch(mcp_server):
    tm = mcp_server._tool_manager
    tagmanager_read = tm._tools["tagmanager_read"].fn

    mock_user = UserContext(
        user_id="user123",
        email="user@example.com",
        display_name="Test User",
        has_adobe_launch=True,
    )
    state.current_user_ctx.set(mock_user)

    mock_launch = AsyncMock()
    mock_launch.list_companies.return_value = {
        "companies": [{"id": "CO1234567890", "name": "Test Company", "org_id": "test_org@AdobeOrg"}],
        "total": 1,
    }
    state.adobe_launch_connector = mock_launch

    with patch(
        "app.tools.tagmanager_tools._get_adobe_launch_conn",
        new=AsyncMock(return_value=("conn_123", "client_id", "client_secret", "org_id")),
    ):
        res = await tagmanager_read(action="list_companies")
        assert res.get("total") == 1
        assert res["companies"][0]["id"] == "CO1234567890"
        assert res["companies"][0]["name"] == "Test Company"


@pytest.mark.asyncio
async def test_tagmanager_read_list_properties_auto_resolve(mcp_server):
    tm = mcp_server._tool_manager
    tagmanager_read = tm._tools["tagmanager_read"].fn

    mock_user = UserContext(
        user_id="user123",
        email="user@example.com",
        display_name="Test User",
        has_adobe_launch=True,
    )
    state.current_user_ctx.set(mock_user)

    mock_launch = AsyncMock()
    mock_launch.list_properties.return_value = {
        "company_id": "CO1234567890",
        "properties": [
            {"id": "PR123", "name": "Test Property", "platform": "web", "domains": ["example.com"]}
        ],
        "total": 1,
    }
    state.adobe_launch_connector = mock_launch

    with patch(
        "app.tools.tagmanager_tools._get_adobe_launch_conn",
        new=AsyncMock(return_value=("conn_123", "client_id", "client_secret", "org_id")),
    ):
        res = await tagmanager_read(action="list_properties", params={"platform": "adobe_launch"})
        assert res.get("total") == 1
        assert res["properties"][0]["id"] == "PR123"


@pytest.mark.asyncio
async def test_adobe_launch_connector_request_headers():
    connector = AdobeLaunchConnector()
    with patch.object(connector, "_get_adobe_token", new=AsyncMock(return_value={"token": "mock_token"})):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)) as mock_get:
            await connector._request(
                client_id="my_client_id",
                client_secret="my_secret",
                org_id="my_org_id@AdobeOrg",
                method="GET",
                endpoint="/companies",
            )
            mock_get.assert_called_once()
            _, kwargs = mock_get.call_args
            headers = kwargs.get("headers", {})
            assert headers.get("x-gw-ims-org-id") == "my_org_id@AdobeOrg"
            assert headers.get("x-api-key") == "my_client_id"
            assert headers.get("Authorization") == "Bearer mock_token"
            assert "application/vnd.api+json" in headers.get("Accept")
