"""Tool-level tests for Branch / AppsFlyer / Adjust branches of the unified marketing tools."""

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

    async def get_app(self, api_key, secret_key):
        self.calls.append(("get_app", api_key, secret_key))
        return {"app": {"app_id": "1"}}

    async def request_daily_export(self, api_key, secret_key, export_date):
        self.calls.append(("request_daily_export", api_key, secret_key, export_date))
        return {"success": True, "export_date": export_date, "files": {}}

    async def query_analytics(self, api_key, secret_key, start_date, end_date, **kwargs):
        self.calls.append(("query_analytics", api_key, secret_key, start_date, end_date, kwargs))
        return {"success": True, "results": []}

    async def list_apps(self, api_key):
        self.calls.append(("list_apps", api_key))
        return {"apps": [], "total": 0}

    async def get_installs_report(self, api_key, app_id, start_date, end_date):
        self.calls.append(("get_installs_report", api_key, app_id, start_date, end_date))
        return {"app_id": app_id, "installs": [], "total": 0}

    async def get_in_app_events_report(self, api_key, app_id, start_date, end_date):
        self.calls.append(("get_in_app_events_report", api_key, app_id, start_date, end_date))
        return {"app_id": app_id, "events": [], "total": 0}

    async def get_partners_report(self, api_key, app_id, start_date, end_date):
        self.calls.append(("get_partners_report", api_key, app_id, start_date, end_date))
        return {"app_id": app_id, "partners": [], "total": 0}

    async def get_report(self, api_key, dimensions, metrics, date_period, **filters):
        self.calls.append(("get_report", api_key, dimensions, metrics, date_period, filters))
        return {"rows": [], "totals": {}}

    async def get_pivot_report(self, api_key, dimensions, metrics, date_period, index, **filters):
        self.calls.append(("get_pivot_report", api_key, dimensions, metrics, date_period, index, filters))
        return {"rows": [], "totals": {}}

    async def list_events(self, api_key):
        self.calls.append(("list_events", api_key))
        return {"events": [], "total": 0}

    async def list_app_automation_apps(self, api_key):
        self.calls.append(("list_app_automation_apps", api_key))
        return {"apps": [], "total": 0}

    async def get_partner_links(self, api_key, app_token):
        self.calls.append(("get_partner_links", api_key, app_token))
        return {"trackers": [], "total": 0}


class _BranchUser:
    user_id = "u1"
    has_branch = True
    has_appsflyer = False
    has_adjust = False


class _AppsFlyerUser:
    user_id = "u1"
    has_branch = False
    has_appsflyer = True
    has_adjust = False


class _AdjustUser:
    user_id = "u1"
    has_branch = False
    has_appsflyer = False
    has_adjust = True


@pytest.fixture
def branch_wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubConnector()
    monkeypatch.setattr(state, "branch_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _BranchUser())

    async def fake_conn(user_id):
        return ("conn-1", "branch-key", "branch-secret")

    monkeypatch.setattr(marketing_tools, "_get_branch_conn", fake_conn)
    return mcp, conn


@pytest.fixture
def appsflyer_wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubConnector()
    monkeypatch.setattr(state, "appsflyer_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _AppsFlyerUser())

    async def fake_conn(user_id):
        return ("conn-1", "af-key", "af-secret")

    monkeypatch.setattr(marketing_tools, "_get_appsflyer_conn", fake_conn)
    return mcp, conn


@pytest.fixture
def adjust_wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubConnector()
    monkeypatch.setattr(state, "adjust_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _AdjustUser())

    async def fake_conn(user_id):
        return ("conn-1", "adj-key", "adj-secret")

    monkeypatch.setattr(marketing_tools, "_get_adjust_conn", fake_conn)
    return mcp, conn


# ---------------------------------------------------------------------------
# Branch marketing_read: get_app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_read_get_app(branch_wired):
    mcp, conn = branch_wired
    result = await mcp.tools["marketing_read"](platform="branch", action="get_app")
    assert conn.calls[0][0] == "get_app"
    assert "app" in result


@pytest.mark.asyncio
async def test_branch_read_unknown_action(branch_wired):
    mcp, conn = branch_wired
    result = await mcp.tools["marketing_read"](platform="branch", action="list_accounts")
    assert result.get("error") is True


# ---------------------------------------------------------------------------
# Branch marketing_write: request_daily_export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_write_request_daily_export(branch_wired):
    mcp, conn = branch_wired
    result = await mcp.tools["marketing_write"](
        platform="branch", action="request_daily_export", export_date="2025-01-15"
    )
    assert conn.calls[0][0] == "request_daily_export"
    assert result["success"] is True
    assert result["export_date"] == "2025-01-15"


@pytest.mark.asyncio
async def test_branch_write_missing_export_date(branch_wired):
    mcp, conn = branch_wired
    result = await mcp.tools["marketing_write"](platform="branch", action="request_daily_export")
    assert result.get("error") is True
    assert "export_date" in result.get("message", "")


# ---------------------------------------------------------------------------
# AppsFlyer marketing_read: list_apps, reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_appsflyer_read_list_apps(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](platform="appsflyer", action="list_apps")
    assert conn.calls[0][0] == "list_apps"


@pytest.mark.asyncio
async def test_appsflyer_read_installs_report(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](
        platform="appsflyer",
        action="get_installs_report",
        app_id="com.example.app",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
    )
    assert conn.calls[0][0] == "get_installs_report"
    assert conn.calls[0][2] == "com.example.app"
    assert conn.calls[0][3] == "2025-01-01"
    assert conn.calls[0][4] == "2025-01-31"
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_appsflyer_read_installs_report_missing_dates(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](
        platform="appsflyer",
        action="get_installs_report",
        app_id="com.example.app",
    )
    assert result.get("error") is True
    assert "date_range_start" in result.get("message", "")


@pytest.mark.asyncio
async def test_appsflyer_read_in_app_events_report(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](
        platform="appsflyer",
        action="get_in_app_events_report",
        app_id="com.example.app",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
    )
    assert conn.calls[0][0] == "get_in_app_events_report"


@pytest.mark.asyncio
async def test_appsflyer_read_partners_report(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](
        platform="appsflyer",
        action="get_partners_report",
        app_id="com.example.app",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
    )
    assert conn.calls[0][0] == "get_partners_report"


@pytest.mark.asyncio
async def test_appsflyer_read_unknown_action(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](platform="appsflyer", action="get_report")
    assert result.get("error") is True


@pytest.mark.asyncio
async def test_appsflyer_read_missing_app_id(appsflyer_wired):
    mcp, conn = appsflyer_wired
    result = await mcp.tools["marketing_read"](
        platform="appsflyer",
        action="get_installs_report",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
    )
    assert result.get("error") is True
    assert "app_id" in result.get("message", "")


# ---------------------------------------------------------------------------
# Adjust marketing_read: list_apps, get_report, get_pivot_report, etc.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjust_read_list_apps(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](platform="adjust", action="list_apps")
    assert conn.calls[0][0] == "list_apps"


@pytest.mark.asyncio
async def test_adjust_read_get_report(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](
        platform="adjust",
        action="get_report",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
        filters={"app_token": "abc123"},
    )
    assert conn.calls[0][0] == "get_report"
    # dimensions/metrics/date_period should be extracted from filters
    _call = conn.calls[0]
    assert _call[2] == "app,tracker"  # default dimensions
    assert _call[3] == "installs,clicks"  # default metrics
    assert "abc123" in str(_call[5])  # extra filter


@pytest.mark.asyncio
async def test_adjust_read_get_report_missing_dates(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](
        platform="adjust",
        action="get_report",
    )
    assert result.get("error") is True
    assert "date_range_start" in result.get("message", "")


@pytest.mark.asyncio
async def test_adjust_read_get_pivot_report(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](
        platform="adjust",
        action="get_pivot_report",
        date_range_start="2025-01-01",
        date_range_end="2025-01-31",
        filters={"index": "campaign"},
    )
    assert conn.calls[0][0] == "get_pivot_report"


@pytest.mark.asyncio
async def test_adjust_read_list_events(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](platform="adjust", action="list_events")
    assert conn.calls[0][0] == "list_events"


@pytest.mark.asyncio
async def test_adjust_read_list_app_automation_apps(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](platform="adjust", action="list_app_automation_apps")
    assert conn.calls[0][0] == "list_app_automation_apps"


@pytest.mark.asyncio
async def test_adjust_read_get_partner_links(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](
        platform="adjust",
        action="get_partner_links",
        resource_id="app-token-123",
    )
    assert conn.calls[0][0] == "get_partner_links"
    assert conn.calls[0][2] == "app-token-123"


@pytest.mark.asyncio
async def test_adjust_read_get_partner_links_missing_resource_id(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](
        platform="adjust",
        action="get_partner_links",
    )
    assert result.get("error") is True
    assert "resource_id" in result.get("message", "")


@pytest.mark.asyncio
async def test_adjust_read_unknown_action(adjust_wired):
    mcp, conn = adjust_wired
    result = await mcp.tools["marketing_read"](platform="adjust", action="get_campaign_performance")
    assert result.get("error") is True


# ---------------------------------------------------------------------------
# Marketing write routing: create_campaign dispatch tests
# ---------------------------------------------------------------------------


class _StubTokenUser:
    user_id = "u1"


class _StubAdsUser:
    user_id = "u1"
    has_ads = True
    connections = []


class _StubWriteConnector:
    """Stub that records calls for platforms that use _get_provider_token."""

    def __init__(self):
        self.calls = []

    async def create_campaign(self, **kwargs):
        self.calls.append(("create_campaign", kwargs))
        return {"campaign_id": "new_camp_001", "updated": True}

    async def update_campaign_budget(self, *args, **kwargs):
        campaign_id = kwargs.get("campaign_id") or (args[2] if len(args) > 2 else None)
        self.calls.append(("update_campaign_budget", {"args": args, "kwargs": kwargs}))
        return {"campaign_id": campaign_id, "updated": True}

    async def update_campaign_status(self, **kwargs):
        self.calls.append(("update_campaign_status", kwargs))
        return {"campaign_id": kwargs.get("campaign_id"), "updated": True}


@pytest.fixture
def write_wired(monkeypatch):
    mcp = marketing_tools
    mcp._tools = {}
    mcp.tools = {}

    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "tiktok_connector", conn, raising=False)
    monkeypatch.setattr(state, "snap_connector", conn, raising=False)
    monkeypatch.setattr(state, "linkedin_connector", conn, raising=False)
    monkeypatch.setattr(state, "pinterest_connector", conn, raising=False)
    monkeypatch.setattr(state, "reddit_connector", conn, raising=False)
    monkeypatch.setattr(state, "apple_connector", conn, raising=False)
    monkeypatch.setattr(state, "meta_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "fake-token")
    monkeypatch.setattr(
        marketing_tools, "_get_provider_oauth1_tokens", lambda p: ("oauth-token", "oauth-secret")
    )
    return conn


@pytest.mark.asyncio
async def test_write_tiktok_create_campaign(write_wired):
    conn = write_wired
    state.tiktok_connector.create_campaign = lambda **kw: _StubWriteConnector().create_campaign(**kw)
    # Use marketing_tools.marketing_write directly (no registration needed)
    # Patch state attributes to point at our stub


@pytest.mark.asyncio
async def test_marketing_write_dispatch_tiktok_create_campaign(monkeypatch):
    """TikTok marketing_write routes create_campaign to connector."""
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "tiktok_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "tiktok-token")

    result = await mcp.tools["marketing_write"](
        platform="tiktok",
        action="create_campaign",
        account_id="acc1",
        campaign_name="Test",
        payload={"objective_type": "TRAFFIC"},
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_snap_create_campaign(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "snap_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "snap-token")

    result = await mcp.tools["marketing_write"](
        platform="snap",
        action="create_campaign",
        account_id="acc1",
        campaign_name="Test",
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_linkedin_create_campaign(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "linkedin_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "linkedin-token")

    result = await mcp.tools["marketing_write"](
        platform="linkedin",
        action="create_campaign",
        account_id="acc1",
        campaign_name="Test",
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_pinterest_create_campaign(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "pinterest_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "pinterest-token")

    result = await mcp.tools["marketing_write"](
        platform="pinterest",
        action="create_campaign",
        account_id="acc1",
        campaign_name="Test",
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_reddit_create_campaign(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "reddit_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "reddit-token")

    result = await mcp.tools["marketing_write"](
        platform="reddit",
        action="create_campaign",
        account_id="acc1",
        campaign_name="Test",
        daily_budget_usd=50.0,
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_apple_create_campaign(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "apple_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "apple-token")

    result = await mcp.tools["marketing_write"](
        platform="apple",
        action="create_campaign",
        account_id="org123",
        campaign_name="Test",
    )
    assert result.get("campaign_id") == "new_camp_001"
    assert conn.calls[0][0] == "create_campaign"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_meta_update_campaign_budget(monkeypatch):
    """Meta marketing_write routes update_campaign_budget to connector."""
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "meta_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "meta-token")

    result = await mcp.tools["marketing_write"](
        platform="meta",
        action="update_campaign_budget",
        account_id="acc1",
        campaign_id="camp1",
        daily_budget_usd=50.0,
    )
    assert result.get("campaign_id") == "camp1"
    assert result.get("updated") is True
    assert conn.calls[0][0] == "update_campaign_budget"


@pytest.mark.asyncio
async def test_marketing_write_dispatch_apple_update_campaign_budget(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    conn = _StubWriteConnector()
    monkeypatch.setattr(state, "apple_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "apple-token")

    result = await mcp.tools["marketing_write"](
        platform="apple",
        action="update_campaign_budget",
        account_id="org123",
        campaign_id="camp1",
        daily_budget_usd=75.0,
    )
    assert result.get("campaign_id") == "camp1"
    assert result.get("updated") is True
    assert conn.calls[0][0] == "update_campaign_budget"


@pytest.mark.asyncio
async def test_marketing_write_unknown_action_returns_error(monkeypatch):
    """Unknown action for a platform returns an error dict."""
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _StubTokenUser())
    monkeypatch.setattr(marketing_tools, "_get_provider_token", lambda p: "tiktok-token")
    monkeypatch.setattr(state, "tiktok_connector", _StubWriteConnector(), raising=False)

    result = await mcp.tools["marketing_write"](
        platform="tiktok",
        action="nonexistent_action",
        account_id="acc1",
    )
    assert result.get("error") is True


# ── Braze ───────────────────────────────────────────────────────────


class _StubBrazeConnector:
    def __init__(self):
        self.calls = []

    async def list_campaigns(self, rest_url, api_key, **kwargs):
        self.calls.append(("list_campaigns", rest_url, api_key, kwargs))
        return {"campaigns": [], "total": 0}

    async def get_campaign_details(self, rest_url, api_key, campaign_id):
        self.calls.append(("get_campaign_details", rest_url, api_key, campaign_id))
        return {"campaign_id": campaign_id, "name": "Test Campaign"}

    async def track_users(self, rest_url, api_key, **kwargs):
        self.calls.append(("track_users", rest_url, api_key, kwargs))
        return {"success": True}

    async def trigger_campaign(self, rest_url, api_key, campaign_id, **kwargs):
        self.calls.append(("trigger_campaign", rest_url, api_key, campaign_id, kwargs))
        return {"success": True, "campaign_id": campaign_id}

    async def create_user_alias(self, rest_url, api_key, **kwargs):
        self.calls.append(("create_user_alias", rest_url, api_key, kwargs))
        return {"success": True}

    async def identify_users(self, rest_url, api_key, **kwargs):
        self.calls.append(("identify_users", rest_url, api_key, kwargs))
        return {"success": True}

    async def merge_users(self, rest_url, api_key, **kwargs):
        self.calls.append(("merge_users", rest_url, api_key, kwargs))
        return {"success": True}


class _BrazeUser:
    user_id = "u1"
    has_braze = True
    has_branch = False
    has_appsflyer = False
    has_adjust = False
    has_moengage = False
    has_marketo = False


@pytest.fixture
def braze_wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubBrazeConnector()
    monkeypatch.setattr(state, "braze_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _BrazeUser())

    async def fake_braze_creds(user_id):
        return {
            "rest_endpoint_url": "https://rest.iad-01.braze.com",
            "api_key": "braze-api-key",
            "connection_id": "conn-1",
            "display_name": "Braze",
        }

    monkeypatch.setattr(marketing_tools, "get_braze_creds", fake_braze_creds)
    return mcp, conn


# Braze marketing_read


@pytest.mark.asyncio
async def test_marketing_read_braze_list_campaigns(braze_wired):
    mcp, conn = braze_wired
    result = await mcp.tools["marketing_read"](platform="braze", action="list_campaigns", filters={"page": 1})
    assert conn.calls[0][0] == "list_campaigns"
    assert conn.calls[0][3]["page"] == 1
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_marketing_read_braze_get_campaign_details(braze_wired):
    mcp, conn = braze_wired
    result = await mcp.tools["marketing_read"](
        platform="braze", action="get_campaign_details", campaign_id="camp-123"
    )
    assert conn.calls[0][0] == "get_campaign_details"
    assert conn.calls[0][3] == "camp-123"
    assert result["campaign_id"] == "camp-123"


@pytest.mark.asyncio
async def test_marketing_read_braze_no_connection_returns_error(braze_wired, monkeypatch):
    mcp, conn = braze_wired

    async def no_creds(user_id):
        return None

    monkeypatch.setattr(marketing_tools, "get_braze_creds", no_creds)
    result = await mcp.tools["marketing_read"](platform="braze", action="list_campaigns")
    assert result.get("error") is True


# Braze marketing_write


@pytest.mark.asyncio
async def test_marketing_write_braze_track_users(braze_wired):
    mcp, conn = braze_wired
    result = await mcp.tools["marketing_write"](
        platform="braze",
        action="track_users",
        payload={"attributes": [{"external_id": "u1", "first_name": "Alice"}]},
    )
    assert conn.calls[0][0] == "track_users"
    assert conn.calls[0][3]["attributes"] == [{"external_id": "u1", "first_name": "Alice"}]
    assert result["success"] is True


@pytest.mark.asyncio
async def test_marketing_write_braze_trigger_campaign(braze_wired):
    mcp, conn = braze_wired
    result = await mcp.tools["marketing_write"](
        platform="braze",
        action="trigger_campaign",
        payload={"campaign_id": "camp-456", "broadcast": True},
    )
    assert conn.calls[0][0] == "trigger_campaign"
    assert conn.calls[0][3] == "camp-456"
    assert conn.calls[0][4]["broadcast"] is True
    assert result["success"] is True


@pytest.mark.asyncio
async def test_marketing_write_braze_no_connection_returns_error(braze_wired, monkeypatch):
    mcp, conn = braze_wired

    async def no_creds(user_id):
        return None

    monkeypatch.setattr(marketing_tools, "get_braze_creds", no_creds)
    result = await mcp.tools["marketing_write"](platform="braze", action="track_users", payload={})
    assert result.get("error") is True


# ── MoEngage ────────────────────────────────────────────────────────


class _StubMoengageConnector:
    def __init__(self):
        self.calls = []

    async def get_user_info(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("get_user_info", dc, app_id, api_key, kwargs))
        return {"user": {"customer_id": kwargs.get("customer_id")}}

    async def list_campaigns(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("list_campaigns", dc, app_id, api_key, kwargs))
        return {"campaigns": [], "total": 0}

    async def create_user(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("create_user", dc, app_id, api_key, kwargs))
        return {"success": True}

    async def send_push(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("send_push", dc, app_id, api_key, kwargs))
        return {"success": True}

    async def list_events(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("list_events", dc, app_id, api_key, kwargs))
        return {"events": [], "total": 0}

    async def add_device(self, dc, app_id, api_key, **kwargs):
        self.calls.append(("add_device", dc, app_id, api_key, kwargs))
        return {"success": True}


class _MoengageUser:
    user_id = "u1"
    has_moengage = True
    has_branch = False
    has_appsflyer = False
    has_adjust = False
    has_braze = False
    has_marketo = False


@pytest.fixture
def moengage_wired(monkeypatch):
    mcp = _StubMCP()
    marketing_tools.register_marketing_tools(mcp)

    conn = _StubMoengageConnector()
    monkeypatch.setattr(state, "moengage_connector", conn, raising=False)
    monkeypatch.setattr(marketing_tools, "_get_user", lambda: _MoengageUser())

    async def fake_moengage_creds(user_id):
        return {
            "data_center": "01",
            "app_id": "moengage-app",
            "api_key": "moengage-api-key",
            "connection_id": "conn-1",
            "display_name": "MoEngage",
        }

    monkeypatch.setattr(marketing_tools, "get_moengage_creds", fake_moengage_creds)
    return mcp, conn


# MoEngage marketing_read


@pytest.mark.asyncio
async def test_marketing_read_moengage_get_user_info(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_read"](
        platform="moengage",
        action="get_user_info",
        filters={"customer_id": "cust-001"},
    )
    assert conn.calls[0][0] == "get_user_info"
    assert conn.calls[0][4]["customer_id"] == "cust-001"
    assert result["user"]["customer_id"] == "cust-001"


@pytest.mark.asyncio
async def test_marketing_read_moengage_list_campaigns(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_read"](
        platform="moengage",
        action="list_campaigns",
        filters={"channel": "push"},
    )
    assert conn.calls[0][0] == "list_campaigns"
    assert conn.calls[0][4]["channel"] == "push"
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_marketing_read_moengage_no_connection_returns_error(moengage_wired, monkeypatch):
    mcp, conn = moengage_wired

    async def no_creds(user_id):
        return None

    monkeypatch.setattr(marketing_tools, "get_moengage_creds", no_creds)
    result = await mcp.tools["marketing_read"](platform="moengage", action="get_user_info")
    assert result.get("error") is True


# MoEngage marketing_write


@pytest.mark.asyncio
async def test_marketing_write_moengage_create_user(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_write"](
        platform="moengage",
        action="create_user",
        payload={"customer_id": "cust-002", "attributes": {"name": "Bob"}},
    )
    assert conn.calls[0][0] == "create_user"
    assert conn.calls[0][4]["customer_id"] == "cust-002"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_marketing_write_moengage_send_push(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_write"](
        platform="moengage",
        action="send_push",
        payload={"campaign_name": "Test Push", "target_platform": ["android"]},
    )
    assert conn.calls[0][0] == "send_push"
    assert conn.calls[0][4]["campaign_name"] == "Test Push"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_marketing_write_moengage_no_connection_returns_error(moengage_wired, monkeypatch):
    mcp, conn = moengage_wired

    async def no_creds(user_id):
        return None

    monkeypatch.setattr(marketing_tools, "get_moengage_creds", no_creds)
    result = await mcp.tools["marketing_write"](platform="moengage", action="create_user", payload={})
    assert result.get("error") is True


@pytest.mark.asyncio
async def test_marketing_read_branch_query_analytics(branch_wired):
    mcp, conn = branch_wired
    result = await mcp.tools["marketing_read"](
        platform="branch",
        action="query_analytics",
        date_range_start="2026-05-01",
        date_range_end="2026-05-31",
    )
    assert conn.calls[0][0] == "query_analytics"
    assert conn.calls[0][3] == "2026-05-01"
    assert conn.calls[0][4] == "2026-05-31"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_marketing_read_moengage_list_events(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_read"](
        platform="moengage",
        action="list_events",
        limit=25,
    )
    assert conn.calls[0][0] == "list_events"
    assert conn.calls[0][4]["limit"] == 25
    assert result["events"] == []


@pytest.mark.asyncio
async def test_marketing_write_braze_aliases_and_merge(braze_wired):
    mcp, conn = braze_wired
    r1 = await mcp.tools["marketing_write"](
        platform="braze",
        action="create_user_alias",
        payload={"user_aliases": [{"alias_name": "a1", "alias_label": "l1"}]},
    )
    assert conn.calls[0][0] == "create_user_alias"
    assert r1["success"] is True

    r2 = await mcp.tools["marketing_write"](
        platform="braze",
        action="identify_users",
        payload={"aliases_to_identify": [{"external_id": "u1"}]},
    )
    assert conn.calls[1][0] == "identify_users"
    assert r2["success"] is True

    r3 = await mcp.tools["marketing_write"](
        platform="braze",
        action="merge_users",
        payload={"merge_updates": [{"identifier_to_merge": {"external_id": "old"}}]},
    )
    assert conn.calls[2][0] == "merge_users"
    assert r3["success"] is True


@pytest.mark.asyncio
async def test_marketing_write_moengage_add_device(moengage_wired):
    mcp, conn = moengage_wired
    result = await mcp.tools["marketing_write"](
        platform="moengage",
        action="add_device",
        payload={"customer_id": "cust-99", "push_token": "token-xyz", "device_platform": "IOS"},
    )
    assert conn.calls[0][0] == "add_device"
    assert conn.calls[0][4]["customer_id"] == "cust-99"
    assert conn.calls[0][4]["push_token"] == "token-xyz"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_unified_routes_dispatch(monkeypatch):
    """Test that unified dispatcher routes newly added actions to legacy tools."""
    from app.tools.unified import MARKETING_READ_ROUTES, MARKETING_WRITE_ROUTES

    assert "appsflyer_list_apps" in MARKETING_READ_ROUTES
    assert "adjust_list_apps" in MARKETING_READ_ROUTES
    assert "branch_get_app" in MARKETING_READ_ROUTES
    assert "branch_query_analytics" in MARKETING_READ_ROUTES
    assert "braze_list_campaigns" in MARKETING_READ_ROUTES
    assert "moengage_list_campaigns" in MARKETING_READ_ROUTES

    assert "branch_request_daily_export" in MARKETING_WRITE_ROUTES
    assert "braze_track_users" in MARKETING_WRITE_ROUTES
    assert "moengage_create_user" in MARKETING_WRITE_ROUTES
    assert "moengage_add_device" in MARKETING_WRITE_ROUTES
