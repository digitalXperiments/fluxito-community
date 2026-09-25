"""Tests for Adobe Launch data elements, rules, rule components, and CRUD operations."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

import app.app_state as state
from app.auth.mcp_session_manager import UserContext
from app.connectors.adobe_launch import AdobeLaunchConnector
from app.tools.registry import register_all_tools


@pytest.fixture
def mcp_server():
    mcp = FastMCP(name="test-fluxito-launch-ops")
    register_all_tools(mcp)
    return mcp


@pytest.fixture
def mock_user_ctx():
    user = UserContext(
        user_id="user_test_launch",
        email="user@example.com",
        display_name="Test User",
        has_adobe_launch=True,
    )
    state.current_user_ctx.set(user)
    return user


# ---------------------------------------------------------------------------
# Connector Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_data_element_attaches_extension_and_stringifies_settings():
    connector = AdobeLaunchConnector()

    with (
        patch.object(connector, "_get_adobe_token", new=AsyncMock(return_value={"token": "mock_token"})),
        patch.object(
            connector,
            "list_extensions",
            new=AsyncMock(
                return_value={
                    "extensions": [
                        {"id": "EX_CORE_123", "name": "core"},
                        {"id": "EX_AA_456", "name": "adobe-analytics"},
                    ]
                }
            ),
        ),
    ):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "data": {
                "id": "DE_TEST_999",
                "type": "data_elements",
                "attributes": {
                    "name": "Page Title",
                    "delegate_descriptor_id": "core::dataElements::javascript-variable",
                    "settings": json.dumps({"path": "document.title"}),
                },
            }
        }

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as mock_post:
            res = await connector.create_data_element(
                client_id="client_123",
                client_secret="secret_123",
                org_id="org_123@AdobeOrg",
                property_id="PR_123",
                name="Page Title",
                delegate_descriptor_id="core::dataElements::javascript-variable",
                settings={"path": "document.title"},
            )

            assert res.get("success") is True
            assert res.get("data_element_id") == "DE_TEST_999"
            assert res.get("extension_id") == "EX_CORE_123"

            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            json_body = kwargs.get("json")

            # Validate JSON:API schema compliance:
            assert json_body["data"]["type"] == "data_elements"
            assert json_body["data"]["attributes"]["name"] == "Page Title"
            assert (
                json_body["data"]["attributes"]["delegate_descriptor_id"]
                == "core::dataElements::javascript-variable"
            )
            # Settings MUST be stringified JSON
            assert isinstance(json_body["data"]["attributes"]["settings"], str)
            assert json.loads(json_body["data"]["attributes"]["settings"]) == {"path": "document.title"}
            # Relationships MUST contain extension with type and id
            assert json_body["data"]["relationships"]["extension"]["data"] == {
                "type": "extensions",
                "id": "EX_CORE_123",
            }


@pytest.mark.asyncio
async def test_create_rule_component_payload_schema():
    connector = AdobeLaunchConnector()

    with (
        patch.object(connector, "_get_adobe_token", new=AsyncMock(return_value={"token": "mock_token"})),
        patch.object(
            connector,
            "list_extensions",
            new=AsyncMock(
                return_value={
                    "extensions": [
                        {"id": "EX_CORE_123", "name": "core"},
                        {"id": "EX_AA_456", "name": "adobe-analytics"},
                    ]
                }
            ),
        ),
    ):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "data": {
                "id": "RC_TEST_111",
                "type": "rule_components",
                "attributes": {
                    "name": "Set eVar1",
                    "delegate_descriptor_id": "adobe-analytics::actions::set-variables",
                },
            }
        }

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as mock_post:
            res = await connector.create_rule_component(
                client_id="client_123",
                client_secret="secret_123",
                org_id="org_123@AdobeOrg",
                property_id="PR_123",
                rule_id="RL_555",
                name="Set eVar1",
                delegate_descriptor_id="adobe-analytics::actions::set-variables",
                settings={"eVar1": "%Page Title%"},
                rule_order=50,
            )

            assert res.get("success") is True
            assert res.get("rule_component_id") == "RC_TEST_111"
            assert res.get("extension_id") == "EX_AA_456"

            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            json_body = kwargs.get("json")

            assert json_body["data"]["type"] == "rule_components"
            assert json_body["data"]["attributes"]["name"] == "Set eVar1"
            assert json_body["data"]["attributes"]["rule_order"] == 50
            assert isinstance(json_body["data"]["attributes"]["settings"], str)
            assert json_body["data"]["relationships"]["rules"]["data"] == [
                {
                    "type": "rules",
                    "id": "RL_555",
                }
            ]
            assert json_body["data"]["relationships"]["extension"]["data"] == {
                "type": "extensions",
                "id": "EX_AA_456",
            }


@pytest.mark.asyncio
async def test_create_rule_with_components():
    connector = AdobeLaunchConnector()

    with patch.object(connector, "_get_adobe_token", new=AsyncMock(return_value={"token": "mock_token"})):
        mock_rule_resp = MagicMock()
        mock_rule_resp.status_code = 201
        mock_rule_resp.json.return_value = {
            "data": {
                "id": "RL_NEW_123",
                "type": "rules",
                "attributes": {"name": "Track Click"},
            }
        }

        with (
            patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_rule_resp)),
            patch.object(
                connector,
                "create_rule_component",
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "rule_component_id": "RC_999",
                        "name": "Click Event",
                    }
                ),
            ) as mock_create_rc,
        ):
            res = await connector.create_rule(
                client_id="client_123",
                client_secret="secret_123",
                org_id="org_123@AdobeOrg",
                property_id="PR_123",
                name="Track Click",
                components=[
                    {
                        "name": "Click Event",
                        "delegate_descriptor_id": "core::events::click",
                        "settings": {"elementSelector": ".button"},
                    }
                ],
            )

            assert res.get("success") is True
            assert res.get("rule_id") == "RL_NEW_123"
            assert res.get("components_count") == 1
            mock_create_rc.assert_called_once()


@pytest.mark.asyncio
async def test_get_rule_returns_components():
    connector = AdobeLaunchConnector()

    with patch.object(connector, "_get_adobe_token", new=AsyncMock(return_value={"token": "mock_token"})):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "id": "RL_123",
                "attributes": {"name": "Voice Player Rule", "enabled": True},
            },
            "included": [
                {
                    "id": "RC_1",
                    "type": "rule_components",
                    "attributes": {
                        "name": "Click Event",
                        "delegate_descriptor_id": "core::events::click",
                        "settings": '{"selector": ".voice"}',
                    },
                },
                {
                    "id": "RC_2",
                    "type": "rule_components",
                    "attributes": {
                        "name": "Set AA Vars",
                        "delegate_descriptor_id": "adobe-analytics::actions::set-variables",
                        "settings": '{"eVar1": "voice"}',
                    },
                },
            ],
        }

        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            res = await connector.get_rule("client_123", "secret_123", "org_123", "RL_123")
            assert res.get("rule_id") == "RL_123"
            assert res.get("components_count") == 2
            assert len(res.get("components", [])) == 2
            assert res["components"][0]["name"] == "Click Event"
            assert res["components"][1]["name"] == "Set AA Vars"


# ---------------------------------------------------------------------------
# MCP Tool Dispatch Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tagmanager_write_create_data_element_dispatch(mcp_server, mock_user_ctx):
    tm = mcp_server._tool_manager
    tagmanager_write = tm._tools["tagmanager_write"].fn

    mock_launch = AsyncMock()
    mock_launch.create_data_element.return_value = {
        "success": True,
        "data_element_id": "DE_12345",
        "name": "eVar1",
        "message": "Data element created successfully",
    }
    state.adobe_launch_connector = mock_launch

    with patch(
        "app.tools.tagmanager_tools._get_adobe_launch_conn",
        new=AsyncMock(return_value=("conn_123", "cid", "sec", "org")),
    ):
        # Auto-routes to adobe_launch even if platform was omitted or gtm
        res = await tagmanager_write(
            action="create_data_element",
            params={
                "config": {
                    "property_id": "PR_123",
                    "name": "eVar1",
                    "delegate_descriptor_id": "core::dataElements::custom-code",
                    "settings": {"source": "return 'test';"},
                }
            },
        )
        assert res.get("success") is True
        assert res.get("data_element_id") == "DE_12345"
        mock_launch.create_data_element.assert_called_once()


@pytest.mark.asyncio
async def test_tagmanager_write_create_rule_component_dispatch(mcp_server, mock_user_ctx):
    tm = mcp_server._tool_manager
    tagmanager_write = tm._tools["tagmanager_write"].fn

    mock_launch = AsyncMock()
    mock_launch.create_rule_component.return_value = {
        "success": True,
        "rule_component_id": "RC_999",
        "rule_id": "RL_555",
        "name": "Custom Code Event",
        "message": "Rule component created successfully",
    }
    state.adobe_launch_connector = mock_launch

    with patch(
        "app.tools.tagmanager_tools._get_adobe_launch_conn",
        new=AsyncMock(return_value=("conn_123", "cid", "sec", "org")),
    ):
        res = await tagmanager_write(
            action="create_rule_component",
            params={
                "config": {
                    "property_id": "PR_123",
                    "rule_id": "RL_555",
                    "name": "Custom Code Event",
                    "delegate_descriptor_id": "core::events::custom-code",
                    "settings": {"source": "window.addEventListener('click', ...)"},
                }
            },
        )
        assert res.get("success") is True
        assert res.get("rule_component_id") == "RC_999"
        mock_launch.create_rule_component.assert_called_once()


@pytest.mark.asyncio
async def test_tagmanager_read_get_data_element_dispatch(mcp_server, mock_user_ctx):
    tm = mcp_server._tool_manager
    tagmanager_read = tm._tools["tagmanager_read"].fn

    mock_launch = AsyncMock()
    mock_launch.get_data_element.return_value = {
        "data_element_id": "DE_123",
        "name": "Page URL",
        "enabled": True,
        "delegate_descriptor_id": "core::dataElements::javascript-variable",
        "settings": '{"path": "window.location.href"}',
    }
    state.adobe_launch_connector = mock_launch

    with patch(
        "app.tools.tagmanager_tools._get_adobe_launch_conn",
        new=AsyncMock(return_value=("conn_123", "cid", "sec", "org")),
    ):
        res = await tagmanager_read(
            action="get_data_element",
            params={"tag_id": "DE_123"},
        )
        assert res.get("data_element_id") == "DE_123"
        assert res.get("name") == "Page URL"
        mock_launch.get_data_element.assert_called_once()
