"""
Regression tests for the MCP tool-call instrumentation hook.

The hook installed by ``registry._install_tool_hook`` is what persists a
``ToolCallAudit`` row for every tool call — the data source behind the
Activity Log page. If the hook is not actually wired onto
``tool_manager.call_tool`` the Activity Log stays permanently empty even
though tools work, which is exactly the bug these tests guard against.
"""

import uuid

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from sqlalchemy import select

from app import app_state
from app.models.audit import ToolCallAudit
from app.models.user import User
from app.tools.registry import _install_tool_hook


def _build_server() -> FastMCP:
    server = FastMCP(name="audit-hook-test")

    @server.tool(name="noop_read", description="returns a plain dict")
    def noop_read() -> dict:
        return {"rows": [1, 2, 3], "error": False}

    @server.tool(name="noop_denied", description="returns a denied error dict")
    def noop_denied() -> dict:
        return {"error": True, "error_type": "scope_denied", "message": "nope"}

    _install_tool_hook(server)
    return server


async def _make_user(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        u = User(email=f"audit-{uuid.uuid4().hex[:8]}@example.com")
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u.id


@pytest.fixture
def _wire_app_state(db_session_factory, fake_redis):
    """Point app_state at the test DB/Redis and clear context after."""
    prev_factory = app_state.db_session_factory
    prev_redis = app_state.redis_client
    app_state.db_session_factory = db_session_factory
    app_state.redis_client = fake_redis
    tokens = []
    try:
        yield
    finally:
        app_state.db_session_factory = prev_factory
        app_state.redis_client = prev_redis
        for var, tok in tokens:
            var.reset(tok)


async def test_hook_writes_audit_row_for_successful_call(db_session_factory, _wire_app_state):
    uid = await _make_user(db_session_factory)
    server = _build_server()

    user_tok = app_state.current_user_ctx.set({"user_id": str(uid)})
    try:
        # Mirror exactly how FastMCP.call_tool invokes the tool manager.
        result = await server._tool_manager.call_tool("noop_read", {}, context=None, convert_result=True)
    finally:
        app_state.current_user_ctx.reset(user_tok)

    # Tool output must be preserved (converted to content blocks).
    assert isinstance(result, list)
    assert isinstance(result[0], TextContent)
    assert '"rows"' in result[0].text

    # An audit row must have been written.
    async with db_session_factory() as db:
        rows = (await db.execute(select(ToolCallAudit).where(ToolCallAudit.user_id == uid))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "noop_read"
    assert row.status == "success"
    # Audit must parse the RAW tool result, not the converted content blocks.
    assert row.response_summary
    assert row.response_preview and '"rows"' in row.response_preview


async def test_hook_records_denied_status_from_raw_result(db_session_factory, _wire_app_state):
    uid = await _make_user(db_session_factory)
    server = _build_server()

    user_tok = app_state.current_user_ctx.set({"user_id": str(uid)})
    try:
        await server._tool_manager.call_tool("noop_denied", {}, context=None, convert_result=True)
    finally:
        app_state.current_user_ctx.reset(user_tok)

    async with db_session_factory() as db:
        row = (await db.execute(select(ToolCallAudit).where(ToolCallAudit.user_id == uid))).scalar_one()
    # scope_denied maps to "denied" — only possible if audit saw the raw dict.
    assert row.status == "denied"
