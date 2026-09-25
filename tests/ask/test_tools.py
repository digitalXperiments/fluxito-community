import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.ask.tools import READ_ONLY_TOOLS, WRITE_REJECTED_MESSAGE, ChatToolBridge, rejection_reason


class _Eff:
    def __init__(self, allowed=None):
        self.allowed = allowed

    def allows_tool(self, name, action=None):
        return self.allowed is None or name in self.allowed


def test_allowlist_is_exactly_the_read_tools():
    assert {
        "analytics_read",
        "tagmanager_read",
        "marketing_read",
        "warehouse_read",
        "seo_read",
        "audience_read",
        "get_knowledge",
        "get_session_context",
        "list_my_projects",
        "run_audit",
        "run_analysis",
    } == READ_ONLY_TOOLS


@pytest.mark.parametrize(
    "name",
    [
        "tagmanager_write",
        "marketing_write",
        "set_active_project",
        "run_script",
        "generic_tool_write",
        "tracking_plan",
    ],
)
def test_non_allowlisted_tools_rejected(name):
    assert rejection_reason(name, {}) is not None


def test_write_actions_of_allowlisted_tools_rejected():
    with patch("app.auth.permissions.is_write_call", return_value=True) as iwc:
        reason = rejection_reason("run_audit", {"action": "tag_save_custom_rule"})
    iwc.assert_called_once_with("run_audit", "tag_save_custom_rule")
    assert reason == WRITE_REJECTED_MESSAGE


def test_real_is_write_call_blocks_run_audit_save_rule():
    from app.auth.permissions import is_write_call

    if not is_write_call("run_audit", "tag_save_custom_rule"):
        pytest.skip("run_audit write actions not registered in this build")
    assert rejection_reason("run_audit", {"action": "tag_save_custom_rule"}) == WRITE_REJECTED_MESSAGE


def test_read_call_allowed_and_rbac_enforced():
    assert rejection_reason("analytics_read", {"action": "run_report"}) is None
    assert rejection_reason("analytics_read", {"action": "run_report"}, _Eff(allowed=set())) is not None
    assert (
        rejection_reason("analytics_read", {"action": "run_report"}, _Eff(allowed={"analytics_read"})) is None
    )
    # Always-on tools skip the RBAC gate.
    assert rejection_reason("list_my_projects", {}, _Eff(allowed=set())) is None


@pytest.mark.asyncio
async def test_dispatch_rejects_before_running():
    bridge = ChatToolBridge("u", "p", eff=None)
    with patch("app.ask.tools.run_mcp_tool", new=AsyncMock()) as run:
        content, is_err = await bridge.dispatch("tagmanager_write", {"action": "publish"})
    run.assert_not_called()
    assert is_err and "not available" in json.loads(content)["error"]

    with (
        patch("app.auth.permissions.is_write_call", return_value=True),
        patch("app.ask.tools.run_mcp_tool", new=AsyncMock()) as run,
    ):
        content, is_err = await bridge.dispatch("run_audit", {"action": "tag_save_custom_rule"})
    run.assert_not_called()
    assert is_err and "read-only" in json.loads(content)["error"]


@pytest.mark.asyncio
async def test_dispatch_runs_allowed_read():
    bridge = ChatToolBridge("u", "p", eff=_Eff())
    with patch("app.ask.tools.run_mcp_tool", new=AsyncMock(return_value=('{"ok": 1}', False))) as run:
        content, is_err = await bridge.dispatch("analytics_read", {"action": "run_report"})
    run.assert_awaited_once_with(
        user_id="u", project_id="p", name="analytics_read", params={"action": "run_report"}
    )
    assert (content, is_err) == ('{"ok": 1}', False)


def test_tool_specs_only_expose_allowlisted_permitted_tools():
    tools = {
        name: SimpleNamespace(name=name, description=f" {name} ", parameters={"type": "object"})
        for name in ("analytics_read", "seo_read", "tagmanager_write", "list_my_projects")
    }
    tm = SimpleNamespace(get_tool=lambda n: tools.get(n))
    fake_main = SimpleNamespace(mcp_server=SimpleNamespace(_tool_manager=tm))
    with patch.dict("sys.modules", {"app.main": fake_main}):
        specs = ChatToolBridge("u", "p", eff=_Eff(allowed={"analytics_read"})).tool_specs()
    assert [s.name for s in specs] == ["analytics_read", "list_my_projects"]
    assert specs[0].description == "analytics_read"
