"""Tool-level tests for the Marketo branch of the unified marketing tools."""

import pytest

import app.app_state as state
from app.tools import marketing_tools


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, name):
        def deco(fn):
            self.tools[name] = fn
            return fn

        return deco


class _StubConnector:
    def __init__(self):
        self.calls = []

    async def get_leads(self, instance_url, client_id, client_secret, **kw):
        self.calls.append(("get_leads", kw))
        return {"result": [{"id": 1}]}

    async def create_or_update_leads(self, instance_url, client_id, client_secret, **kw):
        self.calls.append(("create_or_update_leads", kw))
        return {"result": [{"id": 1, "status": "created"}]}

    async def audit_instance(self, instance_url, client_id, client_secret, **kw):
        self.calls.append(("audit_instance", kw))
        return {"ok": True}

    async def add_leads_to_list(self, instance_url, client_id, client_secret, **kw):
        self.calls.append(("add_leads_to_list", kw))
        return {"result": [{"id": 1, "status": "added"}]}


class _User:
    user_id = "u1"
    has_adobe_marketo = True


@pytest.fixture
def wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubConnector()
    monkeypatch.setattr(state, "adobe_marketo_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _User())

    async def fake_conn(user_id):
        return ("conn-1", "https://x.mktorest.com", "cid", "csecret")

    monkeypatch.setattr(marketing_tools, "_get_marketo_conn", fake_conn)
    return mcp, conn


@pytest.mark.asyncio
async def test_marketing_read_marketo_get_leads(wired):
    mcp, conn = wired
    result = await mcp.tools["marketing_read"](
        platform="marketo",
        action="get_leads",
        filters={"filter_type": "email", "filter_values": ["a@b.com"], "fields": ["id"]},
        limit=5,
    )
    assert result["result"] == [{"id": 1}]
    assert conn.calls[0][0] == "get_leads"
    assert conn.calls[0][1]["filter_type"] == "email"


@pytest.mark.asyncio
async def test_marketing_write_marketo_upsert(wired):
    mcp, conn = wired
    result = await mcp.tools["marketing_write"](
        platform="marketo",
        action="create_or_update_leads",
        payload={"leads": [{"email": "a@b.com"}], "lookup_field": "email"},
    )
    assert result["result"][0]["status"] == "created"
    assert conn.calls[0][0] == "create_or_update_leads"


@pytest.mark.asyncio
async def test_marketing_write_marketo_no_account_id(wired):
    mcp, conn = wired
    result = await mcp.tools["marketing_write"](
        platform="marketo",
        action="add_leads_to_list",
        resource_id="9",
        payload={"lead_ids": ["1", "2"]},
    )
    assert conn.calls[0][0] == "add_leads_to_list"
    assert "error" not in result


@pytest.mark.asyncio
async def test_marketing_audit_marketo(wired):
    mcp, conn = wired
    result = await mcp.tools["marketing_audit"](platform="marketo", action="audit_instance")
    assert conn.calls[0][0] == "audit_instance"
    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Route-table ↔ connector drift guard
# ---------------------------------------------------------------------------
def test_unified_marketo_routes_map_to_real_connector_methods():
    """Every marketo_* flat action routes to (marketing_*, <real connector method>, {platform: marketo}).

    Guards against typos / drift between unified.py routes, the marketing_tools
    branches, and the AdobeMarketoConnector API.
    """
    from app.connectors.adobe_marketo import AdobeMarketoConnector
    from app.tools.unified import (
        AUDIT_ROUTES,
        MARKETING_READ_ROUTES,
        MARKETING_WRITE_ROUTES,
    )

    expected_tool = {
        id(MARKETING_READ_ROUTES): "marketing_read",
        id(MARKETING_WRITE_ROUTES): "marketing_write",
        id(AUDIT_ROUTES): "marketing_audit",
    }
    for routes in (MARKETING_READ_ROUTES, MARKETING_WRITE_ROUTES, AUDIT_ROUTES):
        marketo_routes = {k: v for k, v in routes.items() if k.startswith("marketo_")}
        assert marketo_routes, "expected marketo_* routes in this table"
        for action_name, route in marketo_routes.items():
            legacy_tool, legacy_action, extra = route
            assert legacy_tool == expected_tool[id(routes)], action_name
            assert extra.get("platform") == "marketo", action_name
            assert hasattr(
                AdobeMarketoConnector, legacy_action
            ), f"{action_name} -> connector has no method {legacy_action!r}"
