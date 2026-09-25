"""Tests for Adobe Analytics Workspace project client methods and MCP tools.

HTTP is stubbed with a monkeypatched httpx.AsyncClient — no real network.
Does not require Postgres or Redis (no DB fixtures).
Mirrors tests/test_adobe_marketo_connector.py and tests/test_marketo_tools.py.
"""

from __future__ import annotations

import json

import pytest

from app.connectors.adobe_analytics import AdobeAnalyticsConnector
from app.tools import analytics_tools

_CLIENT_ID = "cid"
_CLIENT_SECRET = "csecret"
_ORG = "ABCDE@AdobeOrg"
_COMPANY = "exampleco"
_TOKEN = "adobe-access-token"
_PROJECT_ID = "6091a10005c7706c0acdd751"
_DISCOVERY = {
    "imsUserId": "user@AdobeID",
    "imsOrgs": [
        {
            "imsOrgId": _ORG,
            "companies": [
                {
                    "globalCompanyId": _COMPANY,
                    "companyName": "Example Co",
                    "apiRateLimitPolicy": "standard",
                }
            ],
        }
    ],
}

_FULL_DEFINITION = {
    "version": "31",
    "workspaces": [{"id": "ws-1", "panels": []}],
    "colorScheme": {"id": "default"},
    "countRepeatInstances": True,
}

_STORED_PROJECT = {
    "id": _PROJECT_ID,
    "name": "Weekly traffic",
    "description": "Original",
    "rsid": "examplersid",
    "owner": {"id": 622291, "name": "Ada"},
    "type": "project",
    "created": "2026-01-01T00:00:00Z",
    "modified": "2026-01-02T00:00:00Z",
    "reportSuiteName": "Example RS",
    "ownerFullName": "Ada Lovelace",
    "definition": _FULL_DEFINITION,
}


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        raise NotImplementedError("post not stubbed")

    async def get(self, url, **kwargs):
        raise NotImplementedError("get not stubbed")

    async def put(self, url, **kwargs):
        raise NotImplementedError("put not stubbed")

    async def delete(self, url, **kwargs):
        raise NotImplementedError("delete not stubbed")


def _resp(status: int, body):
    class _R:
        status_code = status
        text = body if isinstance(body, str) else json.dumps(body)

        def json(self):
            if isinstance(body, str):
                return json.loads(body)
            return body

    return _R()


def _install_client(monkeypatch, *, get=None, post=None, put=None, delete=None):
    calls: list[dict] = []

    class FakeClient(_FakeClientBase):
        async def post(self, url, **kwargs):
            if "/ims/token" in url:
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            calls.append({"method": "POST", "url": url, **kwargs})
            if post is None:
                raise AssertionError(f"unexpected POST {url}")
            return post(url, **kwargs)

        async def get(self, url, **kwargs):
            if "/ims/token" in url:
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            if "/discovery/me" in url:
                return _resp(200, _DISCOVERY)
            calls.append({"method": "GET", "url": url, **kwargs})
            if get is None:
                raise AssertionError(f"unexpected GET {url}")
            return get(url, **kwargs)

        async def put(self, url, **kwargs):
            calls.append({"method": "PUT", "url": url, **kwargs})
            if put is None:
                raise AssertionError(f"unexpected PUT {url}")
            return put(url, **kwargs)

        async def delete(self, url, **kwargs):
            calls.append({"method": "DELETE", "url": url, **kwargs})
            if delete is None:
                raise AssertionError(f"unexpected DELETE {url}")
            return delete(url, **kwargs)

    monkeypatch.setattr("app.connectors.adobe_analytics.httpx.AsyncClient", FakeClient)
    return AdobeAnalyticsConnector(), calls


# ---------------------------------------------------------------------------
# Client: list / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_sends_query_params_and_summarizes(monkeypatch):
    def get(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects"
        params = kwargs.get("params") or {}
        assert params["expansion"] == "reportSuiteName,ownerFullName,definition"
        assert params["includeType"] == "all"
        assert params["limit"] == 3
        assert params["page"] == 1
        assert params["locale"] == "en_US"
        assert "filterByIds" not in params
        assert "ownerId" not in params
        assert "sortProperty" not in params
        assert "sortDirection" not in params
        headers = kwargs.get("headers") or {}
        assert headers["Authorization"] == f"Bearer {_TOKEN}"
        assert headers["x-api-key"] == _CLIENT_ID
        assert headers["x-proxy-global-company-id"] == _COMPANY
        return _resp(
            200,
            {
                "content": [
                    {
                        "id": _PROJECT_ID,
                        "name": "Weekly traffic",
                        "rsid": "examplersid",
                        "owner": {"id": 622291},
                        "ownerFullName": "Ada Lovelace",
                        "reportSuiteName": "Example RS",
                        "type": "project",
                        "created": "2026-01-01T00:00:00Z",
                        "definition": _FULL_DEFINITION,
                    }
                ],
                "totalElements": 94,
                "totalPages": 32,
                "number": 1,
                "size": 3,
                "firstPage": False,
                "lastPage": False,
            },
        )

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.list_projects(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        expansion=["reportSuiteName", "ownerFullName", "definition"],
        include_type="all",
        limit=3,
        page=1,
        locale="en_US",
    )

    assert result["total"] == 94
    assert result["page"] == 1
    assert result["total_pages"] == 32
    assert len(result["projects"]) == 1
    item = result["projects"][0]
    assert item["id"] == _PROJECT_ID
    assert item["name"] == "Weekly traffic"
    assert item["rsid"] == "examplersid"
    assert item["report_suite_name"] == "Example RS"
    assert item["owner"]["name"] == "Ada Lovelace"
    assert item["definition"]["version"] == "31"


@pytest.mark.asyncio
async def test_list_projects_default_expansion_omits_definition(monkeypatch):
    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        assert params["expansion"] == "reportSuiteName,ownerFullName"
        return _resp(
            200,
            {
                "content": [
                    {
                        "id": _PROJECT_ID,
                        "name": "Weekly traffic",
                        "rsid": "examplersid",
                        "definition": _FULL_DEFINITION,
                    }
                ],
                "totalElements": 1,
                "number": 0,
            },
        )

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.list_projects(_CLIENT_ID, _CLIENT_SECRET, _ORG)
    assert "definition" not in result["projects"][0]


@pytest.mark.asyncio
async def test_list_projects_forwards_official_expansion_extras(monkeypatch):
    """Official expansion fields must survive the compact summary (tags/access/refs)."""

    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        assert params["expansion"] == "reportSuiteName,ownerFullName,tags,accessLevel,externalReferences"
        return _resp(
            200,
            {
                "content": [
                    {
                        "id": _PROJECT_ID,
                        "name": "Weekly traffic",
                        "rsid": "examplersid",
                        "tags": [{"id": "t1", "name": "weekly"}],
                        "accessLevel": "Edit",
                        "externalReferences": {"segments": ["s1"]},
                        "shares": [{"shareToId": 7}],
                    }
                ],
                "totalElements": 1,
                "number": 0,
            },
        )

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.list_projects(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        expansion=["reportSuiteName", "ownerFullName", "tags", "accessLevel", "externalReferences"],
    )
    item = result["projects"][0]
    assert item["tags"] == [{"id": "t1", "name": "weekly"}]
    assert item["access_level"] == "Edit"
    assert item["external_references"] == {"segments": ["s1"]}
    assert item["shares"] == [{"shareToId": 7}]
    assert "definition" not in item


@pytest.mark.asyncio
async def test_update_project_requires_writable_field_without_http(monkeypatch):
    conn, calls = _install_client(monkeypatch, put=lambda **k: _resp(500, {}))
    result = await conn.update_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, _PROJECT_ID)
    assert result["error"] is True
    assert result["error_type"] == "invalid_param"
    assert "writable field" in result["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_get_project_always_requests_definition(monkeypatch):
    def get(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"
        assert "definition" in (kwargs.get("params") or {}).get("expansion", "")
        return _resp(200, _STORED_PROJECT)

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.get_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, _PROJECT_ID)
    assert result["id"] == _PROJECT_ID
    assert result["definition"]["workspaces"][0]["id"] == "ws-1"
    assert result["definition"]["colorScheme"]["id"] == "default"


# ---------------------------------------------------------------------------
# Client: create / update (merge) / delete / copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project_posts_writable_fields_only(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _resp(200, {"id": "new-id", "name": "Fresh", "rsid": "examplersid"})

    conn, calls = _install_client(monkeypatch, post=post)
    result = await conn.create_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        name="Fresh",
        rsid="examplersid",
        definition={"version": "31", "customFlag": True, "workspaces": []},
        description="desc",
        extra={
            "shares": [{"shareToId": 1}],
            "owner": {"id": 622291, "name": "Ada"},
            "modified": "2026-01-02T00:00:00Z",
            "reportSuiteName": "Example RS",
        },
    )

    assert captured["url"] == f"https://analytics.adobe.io/api/{_COMPANY}/projects"
    body = captured["json"]
    assert body["name"] == "Fresh"
    assert body["rsid"] == "examplersid"
    assert body["type"] == "project"
    assert body["definition"]["customFlag"] is True
    assert body["shares"] == [{"shareToId": 1}]
    assert "owner" not in body
    assert "modified" not in body
    assert "reportSuiteName" not in body
    assert result["success"] is True
    assert result["project_id"] == "new-id"
    assert [c["method"] for c in calls] == ["POST"]


@pytest.mark.asyncio
async def test_update_project_name_only_is_single_partial_put(monkeypatch):
    """A rename is PUT {\"name\": ...} with no prior GET and no other keys."""

    def put(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"
        return _resp(200, {"id": _PROJECT_ID, "name": "Renamed", "rsid": "examplersid"})

    conn, calls = _install_client(monkeypatch, put=put)
    result = await conn.update_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        _PROJECT_ID,
        name="Renamed",
    )

    assert result["success"] is True
    assert result["project_id"] == _PROJECT_ID
    assert result["updated_fields"] == ["name"]
    assert result["merged_definition"] is False
    assert [c["method"] for c in calls] == ["PUT"]
    body = calls[0]["json"]
    assert body == {"name": "Renamed"}
    assert set(body) == {"name"}


@pytest.mark.asyncio
async def test_update_project_definition_without_merge_flag_is_partial_put(monkeypatch):
    def put(url, **kwargs):
        return _resp(200, {"id": _PROJECT_ID})

    conn, calls = _install_client(monkeypatch, put=put)
    await conn.update_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        _PROJECT_ID,
        definition={"version": "32"},
    )
    assert [c["method"] for c in calls] == ["PUT"]
    assert calls[0]["json"] == {"definition": {"version": "32"}}


@pytest.mark.asyncio
async def test_update_project_merge_definition_puts_merged_subtree_only(monkeypatch):
    """Opt-in merge GETs definition, then PUTs merged definition + caller fields."""

    def get(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"
        return _resp(200, _STORED_PROJECT)

    def put(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"
        return _resp(200, {"id": _PROJECT_ID, "name": "Renamed"})

    conn, calls = _install_client(monkeypatch, get=get, put=put)
    result = await conn.update_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        _PROJECT_ID,
        name="Renamed",
        definition={"version": "32"},
        merge_definition=True,
    )

    assert result["success"] is True
    assert result["merged_definition"] is True
    assert [c["method"] for c in calls] == ["GET", "PUT"]

    body = calls[1]["json"]
    assert set(body) == {"name", "definition"}
    assert body["name"] == "Renamed"
    assert body["definition"]["version"] == "32"
    assert body["definition"]["workspaces"] == _FULL_DEFINITION["workspaces"]
    assert body["definition"]["colorScheme"] == _FULL_DEFINITION["colorScheme"]
    assert body["definition"]["countRepeatInstances"] is True
    for server_key in ("owner", "type", "modified", "created", "id", "rsid", "description"):
        assert server_key not in body


@pytest.mark.asyncio
async def test_update_project_whitelists_writable_extra_keys(monkeypatch):
    def put(url, **kwargs):
        return _resp(200, {"id": _PROJECT_ID})

    conn, calls = _install_client(monkeypatch, put=put)
    await conn.update_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        _PROJECT_ID,
        updates={"shares": [{"shareToId": 7}], "customTopLevel": True, "modified": "nope"},
    )
    assert [c["method"] for c in calls] == ["PUT"]
    body = calls[0]["json"]
    assert body == {"shares": [{"shareToId": 7}]}
    assert "customTopLevel" not in body
    assert "modified" not in body
    assert "definition" not in body


@pytest.mark.asyncio
async def test_delete_project_requires_explicit_id_and_calls_delete(monkeypatch):
    def delete(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"
        return _resp(200, {"result": "success"})

    conn, calls = _install_client(monkeypatch, delete=delete)
    result = await conn.delete_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, _PROJECT_ID)
    assert result["success"] is True
    assert result["project_id"] == _PROJECT_ID
    assert [c["method"] for c in calls] == ["DELETE"]
    assert calls[0]["url"] == f"https://analytics.adobe.io/api/{_COMPANY}/projects/{_PROJECT_ID}"

    empty = await conn.delete_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, "  ")
    assert empty["error"] is True
    assert empty["error_type"] == "invalid_param"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_copy_project_gets_then_posts_without_source_id(monkeypatch):
    captured = {}

    def get(url, **kwargs):
        return _resp(200, _STORED_PROJECT)

    def post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _resp(200, {"id": "copy-id", "name": "Weekly traffic (copy)", "rsid": "examplersid"})

    conn, calls = _install_client(monkeypatch, get=get, post=post)
    result = await conn.copy_project(
        _CLIENT_ID, _CLIENT_SECRET, _ORG, _PROJECT_ID, name="Weekly traffic (copy)"
    )
    assert result["success"] is True
    assert result["project_id"] == "copy-id"
    assert result["copied_from"] == _PROJECT_ID
    body = captured["json"]
    assert body["name"] == "Weekly traffic (copy)"
    assert body.get("id") != _PROJECT_ID
    assert "id" not in body
    assert body["definition"] == _FULL_DEFINITION
    assert body["rsid"] == "examplersid"
    assert body.get("description") == "Original"
    for server_key in ("owner", "created", "modified", "reportSuiteName", "ownerFullName"):
        assert server_key not in body
    assert [c["method"] for c in calls] == ["GET", "POST"]


@pytest.mark.parametrize("bad_id", ["*", "a/b", "../projects", "", "  ", "proj?x=1"])
@pytest.mark.parametrize("method", ["get", "update", "delete", "copy"])
@pytest.mark.asyncio
async def test_invalid_project_id_makes_zero_http_calls(monkeypatch, method, bad_id):
    def boom(url, **kwargs):
        raise AssertionError(f"HTTP must not fire for invalid id {bad_id!r}: {url}")

    conn, calls = _install_client(monkeypatch, get=boom, post=boom, put=boom, delete=boom)
    if method == "get":
        result = await conn.get_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, bad_id)
    elif method == "update":
        result = await conn.update_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, bad_id, name="Nope")
    elif method == "delete":
        result = await conn.delete_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, bad_id)
    else:
        result = await conn.copy_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, bad_id, name="Nope")

    assert result["error"] is True
    assert result["error_type"] == "invalid_param"
    assert "project_id" in result["message"]
    assert calls == []


# ---------------------------------------------------------------------------
# Client: Adobe error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_404_maps_to_actionable_error(monkeypatch):
    def get(url, **kwargs):
        return _resp(404, {"errorCode": "project_not_found", "errorDescription": "No such project"})

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.get_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, "missing")
    assert result["error"] is True
    assert result["status_code"] == 404
    assert result["error_type"] == "invalid_param"
    assert result["adobe_error_code"] == "project_not_found"
    assert "No such project" in result["adobe_error_message"]
    assert "Unknown" in result["message"]


@pytest.mark.asyncio
async def test_get_project_403_maps_to_insufficient_scope(monkeypatch):
    def get(url, **kwargs):
        return _resp(403, {"errorCode": "forbidden", "errorDescription": "Not permitted"})

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.get_project(_CLIENT_ID, _CLIENT_SECRET, _ORG, _PROJECT_ID)
    assert result["error"] is True
    assert result["status_code"] == 403
    assert result["error_type"] == "insufficient_scope"
    assert result["adobe_error_code"] == "forbidden"


@pytest.mark.asyncio
async def test_create_project_400_includes_adobe_error_code(monkeypatch):
    def post(url, **kwargs):
        return _resp(
            400,
            {"errorCode": "invalid_definition", "errorDescription": "workspaces is required"},
        )

    conn, _ = _install_client(monkeypatch, post=post)
    result = await conn.create_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        name="Bad",
        rsid="examplersid",
        definition={"version": "31"},
    )
    assert result["error"] is True
    assert result["status_code"] == 400
    assert result["error_type"] == "invalid_param"
    assert result["adobe_error_code"] == "invalid_definition"
    assert "workspaces is required" in result["message"]


@pytest.mark.asyncio
async def test_list_projects_429_maps_to_upstream_error(monkeypatch):
    def get(url, **kwargs):
        return _resp(429, {"errorCode": "rate_limited", "errorDescription": "Too many requests"})

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.list_projects(_CLIENT_ID, _CLIENT_SECRET, _ORG)
    assert result["error"] is True
    assert result["status_code"] == 429
    assert result["error_type"] == "upstream_error"
    assert result["adobe_error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_create_project_rejects_empty_name_without_http(monkeypatch):
    conn, calls = _install_client(monkeypatch, post=lambda **k: _resp(500, {}))
    result = await conn.create_project(
        _CLIENT_ID, _CLIENT_SECRET, _ORG, name="  ", rsid="rsid", definition={}
    )
    assert result["error"] is True
    assert result["error_type"] == "invalid_param"
    assert calls == []


# ---------------------------------------------------------------------------
# MCP tools (analytics_read / analytics_write Adobe branch)
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, name):
        def deco(fn):
            self.tools[name] = fn
            return fn

        return deco


class _User:
    user_id = "u1"
    has_adobe_analytics = True


class _StubAdobe:
    def __init__(self):
        self.calls: list[tuple] = []

    async def list_projects(self, *args, **kwargs):
        self.calls.append(("list_projects", args, kwargs))
        return {"projects": [{"id": _PROJECT_ID, "name": "Weekly traffic"}], "total": 1}

    async def get_project(self, *args, **kwargs):
        self.calls.append(("get_project", args, kwargs))
        return dict(_STORED_PROJECT)

    async def create_project(self, *args, **kwargs):
        self.calls.append(("create_project", args, kwargs))
        return {"success": True, "project_id": "new-id", "name": kwargs["name"]}

    async def update_project(self, *args, **kwargs):
        self.calls.append(("update_project", args, kwargs))
        return {
            "success": True,
            "project_id": args[3],
            "updated_fields": ["name"],
            "merged_definition": bool(kwargs.get("merge_definition")),
        }

    async def delete_project(self, *args, **kwargs):
        self.calls.append(("delete_project", args, kwargs))
        return {"success": True, "project_id": args[3]}

    async def copy_project(self, *args, **kwargs):
        self.calls.append(("copy_project", args, kwargs))
        return {"success": True, "project_id": "copy-id", "copied_from": args[3]}

    async def build_project_definition(self, *args, **kwargs):
        self.calls.append(("build_project_definition", args, kwargs))
        rsid = kwargs.get("rsid") or (args[0] if args else "rsid")
        return {"rsid": rsid, "definition": {"version": "31", "workspaces": [{"panels": []}]}}

    async def validate_project(self, *args, **kwargs):
        self.calls.append(("validate_project", args, kwargs))
        return {"valid": True, "rsid": kwargs.get("rsid")}


@pytest.fixture
def wired_tools(monkeypatch):
    import app.app_state as state

    mcp = _StubMCP()
    analytics_tools.register_analytics_tools(mcp)
    stub = _StubAdobe()
    monkeypatch.setattr(state, "adobe_analytics_connector", stub, raising=False)
    monkeypatch.setattr(analytics_tools, "_user", lambda: _User())

    async def fake_conn(user_id):
        return ("conn-1", _CLIENT_ID, _CLIENT_SECRET, _ORG, _COMPANY)

    monkeypatch.setattr(analytics_tools, "_get_adobe_conn", fake_conn)
    return mcp, stub


@pytest.mark.asyncio
async def test_analytics_read_list_projects_forwards_filters(wired_tools):
    mcp, stub = wired_tools
    result = await mcp.tools["analytics_read"](
        platform="adobe_analytics",
        action="list_projects",
        expansion=["reportSuiteName"],
        include_type="shared",
        page=2,
        limit=10,
        config={"locale": "en_US"},
    )
    assert result["total"] == 1
    assert stub.calls[0][0] == "list_projects"
    kwargs = stub.calls[0][2]
    assert kwargs["expansion"] == ["reportSuiteName"]
    assert kwargs["include_type"] == "shared"
    assert kwargs["page"] == 2
    assert kwargs["limit"] == 10
    assert kwargs["locale"] == "en_US"
    assert "filter_by_ids" not in kwargs
    assert "owner_id" not in kwargs
    assert "sort" not in kwargs


@pytest.mark.asyncio
async def test_analytics_read_get_project_requires_id(wired_tools):
    mcp, stub = wired_tools
    missing = await mcp.tools["analytics_read"](platform="adobe_analytics", action="get_project")
    assert missing["error"] is True
    assert missing["error_type"] == "missing_required_param"
    assert stub.calls == []

    result = await mcp.tools["analytics_read"](
        platform="adobe_analytics", action="get_project", project_id=_PROJECT_ID
    )
    assert result["id"] == _PROJECT_ID
    assert stub.calls[0][0] == "get_project"
    assert stub.calls[0][1][3] == _PROJECT_ID


@pytest.mark.asyncio
async def test_analytics_write_create_update_delete_copy(wired_tools):
    mcp, stub = wired_tools
    write = mcp.tools["analytics_write"]

    created = await write(
        platform="adobe_analytics",
        action="create_project",
        config={"name": "Fresh", "rsid": "examplersid", "definition": {"version": "31"}},
    )
    assert created["success"] is True
    assert stub.calls[-1][0] == "create_project"
    assert stub.calls[-1][2]["definition"]["version"] == "31"

    updated = await write(
        platform="adobe_analytics",
        action="update_project",
        config={
            "project_id": _PROJECT_ID,
            "name": "Renamed",
            "shares": [{"shareToId": 1}],
            "merge_definition": True,
            "owner_full_name": "should-not-forward",
        },
    )
    assert updated["success"] is True
    upd = stub.calls[-1]
    assert upd[0] == "update_project"
    assert upd[1][3] == _PROJECT_ID
    assert upd[2]["name"] == "Renamed"
    assert upd[2]["updates"] == {"shares": [{"shareToId": 1}]}
    assert upd[2]["merge_definition"] is True

    deleted = await write(
        platform="adobe_analytics",
        action="delete_project",
        config={"project_id": _PROJECT_ID},
    )
    assert deleted["success"] is True
    assert stub.calls[-1][0] == "delete_project"

    copied = await write(
        platform="adobe_analytics",
        action="copy_project",
        config={"project_id": _PROJECT_ID, "name": "Copy"},
    )
    assert copied["copied_from"] == _PROJECT_ID
    assert stub.calls[-1][0] == "copy_project"


@pytest.mark.asyncio
async def test_analytics_write_delete_project_rejects_missing_id(wired_tools):
    mcp, stub = wired_tools
    result = await mcp.tools["analytics_write"](
        platform="adobe_analytics", action="delete_project", config={}
    )
    assert result["error"] is True
    assert result["error_type"] == "missing_required_param"
    assert stub.calls == []


@pytest.mark.parametrize("bad_id", ["*", "a/b", "../projects"])
@pytest.mark.asyncio
async def test_analytics_write_rejects_unsafe_project_ids(wired_tools, bad_id):
    mcp, stub = wired_tools
    for action in ("update_project", "delete_project", "copy_project"):
        config = {"project_id": bad_id}
        if action != "delete_project":
            config["name"] = "Nope"
        result = await mcp.tools["analytics_write"](platform="adobe_analytics", action=action, config=config)
        assert result["error"] is True, action
        assert result["error_type"] == "invalid_param", action
        assert "project_id" in result["message"]
    assert stub.calls == []


@pytest.mark.asyncio
async def test_analytics_read_rejects_unsafe_project_id(wired_tools):
    mcp, stub = wired_tools
    result = await mcp.tools["analytics_read"](
        platform="adobe_analytics", action="get_project", project_id="../projects"
    )
    assert result["error"] is True
    assert result["error_type"] == "invalid_param"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_analytics_write_create_project_rejects_non_object_definition(wired_tools):
    mcp, stub = wired_tools
    result = await mcp.tools["analytics_write"](
        platform="adobe_analytics",
        action="create_project",
        config={"name": "X", "rsid": "rs", "definition": "not-an-object"},
    )
    assert result["error"] is True
    assert result["error_type"] == "invalid_param"
    assert stub.calls == []


def test_unified_project_routes_map_to_connector_methods():
    from app.connectors.adobe_analytics import AdobeAnalyticsConnector
    from app.tools.unified import ANALYTICS_READ_ROUTES, ANALYTICS_WRITE_ROUTES

    for action in (
        "adobe_workspace_list_projects",
        "adobe_workspace_get_project",
        "adobe_workspace_build_definition",
        "adobe_workspace_validate_project",
    ):
        tool, legacy = ANALYTICS_READ_ROUTES[action]
        assert tool == "analytics_read"
        assert hasattr(AdobeAnalyticsConnector, legacy)

    for action in (
        "adobe_workspace_create_project",
        "adobe_workspace_update_project",
        "adobe_workspace_delete_project",
        "adobe_workspace_copy_project",
    ):
        tool, legacy = ANALYTICS_WRITE_ROUTES[action]
        assert tool == "analytics_write"
        assert hasattr(AdobeAnalyticsConnector, legacy)


@pytest.mark.asyncio
async def test_create_project_expands_incomplete_definition(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _resp(200, {"id": "new-id", "name": "Fresh", "rsid": "examplersid"})

    conn, _ = _install_client(monkeypatch, post=post)
    result = await conn.create_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        name="Fresh",
        rsid="examplersid",
        definition={"version": "31"},
    )
    assert result["success"] is True
    definition = captured["json"]["definition"]
    assert definition["version"] == "31"
    assert definition["workspaces"][0]["panels"]
    assert definition["workspaces"][0]["panels"][0]["reportSuite"]["id"] == "examplersid"


@pytest.mark.asyncio
async def test_create_project_from_tables_builds_definition(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _resp(200, {"id": "tbl-id", "name": "Traffic", "rsid": "examplersid"})

    conn, _ = _install_client(monkeypatch, post=post)
    result = await conn.create_project(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        name="Traffic",
        rsid="examplersid",
        tables=[{"name": "Pages", "metrics": ["visits", "pageviews"], "dimension": "page"}],
        date_range="thisMonth",
    )
    assert result["success"] is True
    assert captured["url"] == f"https://analytics.adobe.io/api/{_COMPANY}/projects"
    body = captured["json"]
    assert body["name"] == "Traffic"
    assert "workspaces" in body["definition"]
    reportlet = body["definition"]["workspaces"][0]["panels"][0]["subPanels"][0]["reportlet"]
    metric_ids = [n["component"]["id"] for n in reportlet["columnTree"]["nodes"]]
    assert "metrics/visits" in metric_ids
    assert "metrics/pageviews" in metric_ids


@pytest.mark.asyncio
async def test_analytics_write_create_project_accepts_tables(wired_tools):
    mcp, stub = wired_tools
    created = await mcp.tools["analytics_write"](
        platform="adobe_analytics",
        action="create_project",
        config={
            "name": "Traffic",
            "rsid": "examplersid",
            "tables": [{"metrics": ["visits"], "dimension": "page"}],
        },
    )
    assert created["success"] is True
    assert stub.calls[-1][0] == "create_project"
    assert stub.calls[-1][2]["tables"][0]["metrics"] == ["visits"]


@pytest.mark.asyncio
async def test_analytics_read_build_and_validate_actions(wired_tools):
    mcp, stub = wired_tools
    built = await mcp.tools["analytics_read"](
        platform="adobe_analytics",
        action="build_definition",
        config={"rsid": "examplersid", "tables": [{"metrics": ["visits"]}]},
    )
    assert built["definition"]["version"] == "31"
    assert stub.calls[0][0] == "build_project_definition"

    checked = await mcp.tools["analytics_read"](
        platform="adobe_analytics",
        action="validate_project",
        config={"rsid": "examplersid", "tables": [{"metrics": ["visits"]}]},
    )
    assert checked["valid"] is True
    assert stub.calls[-1][0] == "validate_project"
