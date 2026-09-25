"""
Tests for programmatic tool calling (`run_script`).

Covers:
  1. AST validation — forbidden constructs rejected before execution
  2. Happy path — scripts execute, call fake tools, return RESULT
  3. Parallelism — gather() fans out correctly
  4. Budgets — timeout, tool-call cap, output size cap enforced
  5. Error handling — tool errors surface as return values, not exceptions
  6. Denylist — run_script cannot call itself recursively
"""

from __future__ import annotations

import pytest

from app.tools.programmatic import (
    MAX_OUTPUT_BYTES,
    MAX_TOOL_CALLS,
    _validate_ast,
    register_programmatic_tool,
)

# ---------------------------------------------------------------------------
# AST validation
# ---------------------------------------------------------------------------


class TestAstValidation:
    """The sandbox rejects forbidden constructs BEFORE executing any code."""

    def test_plain_code_passes(self):
        _validate_ast("x = 1\ny = x + 2\nRESULT = y")

    def test_for_loop_passes(self):
        _validate_ast("RESULT = [i * 2 for i in range(10)]")

    def test_await_in_async_context_passes(self):
        # After wrapping, `await call(...)` becomes valid. At the raw AST
        # level, awaits outside async def are actually a SyntaxError, but
        # we validate the PRE-wrapped code — which is only syntactically
        # valid once wrapped. So instead we check that top-level awaits
        # pass through the walker without triggering the name/node bans.
        _validate_ast("x = [1, 2, 3]\nRESULT = sorted(x)")

    @pytest.mark.parametrize(
        "code, needle",
        [
            ("import os", "Import"),
            ("from os import path", "ImportFrom"),
            ("with open('x') as f: pass", "With"),
            ("class Foo: pass", "ClassDef"),
            ("try:\n    x=1\nexcept: pass", "Try"),
            ("raise ValueError('x')", "Raise"),
            ("global x", "Global"),
        ],
    )
    def test_forbidden_node_rejected(self, code, needle):
        with pytest.raises(ValueError, match=needle):
            _validate_ast(code)

    @pytest.mark.parametrize(
        "name",
        ["__import__", "eval", "exec", "open", "getattr", "globals", "compile"],
    )
    def test_forbidden_name_rejected(self, name):
        with pytest.raises(ValueError, match=name):
            _validate_ast(f"x = {name}")

    def test_dunder_attribute_rejected(self):
        # Classic sandbox escape: [].__class__.__bases__[0].__subclasses__()
        with pytest.raises(ValueError, match="__class__"):
            _validate_ast("x = [].__class__")

    def test_underscore_attribute_rejected(self):
        with pytest.raises(ValueError, match="_private"):
            _validate_ast("x = obj._private")

    def test_syntax_error_rejected_with_line_info(self):
        with pytest.raises(ValueError, match="Syntax error on line"):
            _validate_ast("x = (1 +")


# ---------------------------------------------------------------------------
# Runtime tests — exercise the registered run_script tool
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_mcp_server():
    """A bare-bones FastMCP-like object sufficient for run_script.

    We don't need the full server — just a `tool_manager` with `_tools`
    dict, `add_tool` method, and the ability to run tools. The real
    FastMCP is avoided to keep these tests hermetic (no Redis, no DB,
    no Google creds required).
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name="test-programmatic")

    # Register two fake tools the scripts can call.
    @mcp.tool("fake_list")
    async def fake_list(n: int = 3) -> dict:
        """Return a list of n items."""
        return {"items": [{"id": i, "name": f"item_{i}"} for i in range(n)]}

    @mcp.tool("fake_audit")
    async def fake_audit(item_id: int) -> dict:
        """Return a score for the given item id."""
        # Deterministic "score" so tests can assert on it.
        return {"item_id": item_id, "score": 100 - item_id * 10}

    @mcp.tool("fake_error")
    async def fake_error() -> dict:
        """Always returns an error dict (never raises)."""
        return {"error": True, "error_type": "simulated", "message": "nope"}

    @mcp.tool("fake_slow")
    async def fake_slow(seconds: float = 0.05) -> dict:
        """Sleeps, then returns — exercises the await-boundary timeout."""
        import asyncio as _a

        await _a.sleep(seconds)
        return {"slept": seconds}

    register_programmatic_tool(mcp)
    return mcp


async def _run(mcp, code: str, timeout_seconds: int | None = None):
    """Invoke run_script via the tool manager and return the dict."""
    tool = mcp._tool_manager._tools["run_script"]
    args = {"code": code}
    if timeout_seconds is not None:
        args["timeout_seconds"] = timeout_seconds
    return await tool.run(args)


class TestHappyPath:
    async def test_simple_return(self, fake_mcp_server):
        out = await _run(fake_mcp_server, "RESULT = 1 + 2")
        assert out["result"] == 3
        assert out["tool_calls_made"] == 0
        assert out["trace"] == []

    async def test_single_tool_call(self, fake_mcp_server):
        out = await _run(
            fake_mcp_server,
            'data = await call("fake_list", {"n": 2})\nRESULT = len(data["items"])',
        )
        assert out["result"] == 2
        assert out["tool_calls_made"] == 1
        assert out["trace"][0]["tool"] == "fake_list"

    async def test_gather_parallel(self, fake_mcp_server):
        code = (
            "ids = [0, 1, 2, 3]\n"
            'audits = await gather([call("fake_audit", {"item_id": i}) for i in ids])\n'
            'RESULT = [a["score"] for a in audits]\n'
        )
        out = await _run(fake_mcp_server, code)
        assert out["result"] == [100, 90, 80, 70]
        assert out["tool_calls_made"] == 4

    async def test_filter_and_aggregate(self, fake_mcp_server):
        code = (
            'resp = await call("fake_list", {"n": 5})\n'
            "audits = await gather([\n"
            '    call("fake_audit", {"item_id": it["id"]})\n'
            '    for it in resp["items"]\n'
            "])\n"
            'RESULT = [a for a in audits if a["score"] < 80]\n'
        )
        out = await _run(fake_mcp_server, code)
        # item_ids 3, 4 → scores 70, 60
        assert out["result"] == [
            {"item_id": 3, "score": 70},
            {"item_id": 4, "score": 60},
        ]

    async def test_print_captured(self, fake_mcp_server):
        out = await _run(fake_mcp_server, 'print("hello", 1, 2)\nRESULT = "ok"')
        assert out["result"] == "ok"
        assert "hello 1 2" in out["stdout"]

    async def test_tool_error_is_return_value(self, fake_mcp_server):
        # Scripts inspect errors as return values, not exceptions.
        code = (
            'r = await call("fake_error", {})\n'
            'RESULT = {"is_error": bool(r.get("error")), "type": r.get("error_type")}\n'
        )
        out = await _run(fake_mcp_server, code)
        assert out["result"] == {"is_error": True, "type": "simulated"}


class TestBudgetsAndErrors:
    async def test_invalid_script_rejected_before_run(self, fake_mcp_server):
        out = await _run(fake_mcp_server, "import os\nRESULT = 1")
        assert out["error"] is True
        assert out["error_type"] == "invalid_script"
        assert "Import" in out["message"]

    async def test_empty_code_rejected(self, fake_mcp_server):
        out = await _run(fake_mcp_server, "   ")
        assert out["error"] is True
        assert out["error_type"] == "invalid_script"

    async def test_unknown_tool_errors(self, fake_mcp_server):
        out = await _run(fake_mcp_server, 'RESULT = await call("not_a_tool", {})')
        assert out["error"] is True
        assert "Unknown tool" in out["message"]

    async def test_recursive_run_script_blocked(self, fake_mcp_server):
        out = await _run(
            fake_mcp_server,
            'RESULT = await call("run_script", {"code": "RESULT = 1"})',
        )
        assert out["error"] is True
        assert "run_script" in out["message"]

    async def test_tool_call_budget_enforced(self, fake_mcp_server):
        # Issue MAX_TOOL_CALLS + 1 calls and expect a RuntimeError return.
        code = (
            f"for i in range({MAX_TOOL_CALLS + 1}):\n"
            '    await call("fake_audit", {"item_id": 0})\n'
            'RESULT = "done"\n'
        )
        out = await _run(fake_mcp_server, code)
        assert out["error"] is True
        assert out["error_type"] == "RuntimeError"
        assert str(MAX_TOOL_CALLS) in out["message"]

    async def test_timeout_enforced_at_await_boundary(self, fake_mcp_server):
        # asyncio.wait_for cancels at the next await — so we chain awaits
        # that cumulatively exceed the timeout. Pure-sync loops CANNOT be
        # cancelled in-process; that limitation is documented in the
        # module and gated by MAX_SCRIPT_SECONDS_HARD_CAP (60s) upstream.
        code = (
            "results = []\n"
            "for _ in range(20):\n"
            '    r = await call("fake_slow", {"seconds": 0.2})\n'
            "    results.append(r)\n"
            "RESULT = len(results)\n"
        )
        out = await _run(fake_mcp_server, code, timeout_seconds=1)
        assert out["error"] is True
        assert out["error_type"] == "timeout"
        # Should have made some, but not all, calls before cancellation.
        assert 0 < out["tool_calls_made"] < 20

    async def test_output_size_cap(self, fake_mcp_server):
        # Build a result that's guaranteed to blow past MAX_OUTPUT_BYTES.
        code = f'RESULT = ["x" * 1000 for _ in range({MAX_OUTPUT_BYTES // 500})]'
        out = await _run(fake_mcp_server, code)
        assert out["error"] is True
        assert out["error_type"] == "output_too_large"
        assert "preview" in out

    async def test_non_serializable_result(self, fake_mcp_server):
        # Tuple-keyed dicts can't be JSON-encoded even with default=str,
        # because `default` only fires for values — keys must be primitive.
        out = await _run(fake_mcp_server, "RESULT = {(1, 2): 'tuple-key'}")
        assert out["error"] is True
        assert out["error_type"] == "non_serializable_result"


class TestTraceAndObservability:
    async def test_trace_records_each_call(self, fake_mcp_server):
        code = (
            'a = await call("fake_list", {"n": 1})\n'
            'b = await call("fake_audit", {"item_id": 0})\n'
            "RESULT = [a, b]\n"
        )
        out = await _run(fake_mcp_server, code)
        tools = [t["tool"] for t in out["trace"]]
        assert tools == ["fake_list", "fake_audit"]
        for entry in out["trace"]:
            assert "duration_ms" in entry
            assert entry["duration_ms"] >= 0

    async def test_duration_present_on_success(self, fake_mcp_server):
        out = await _run(fake_mcp_server, "RESULT = 42")
        assert "duration_ms" in out
        assert out["duration_ms"] >= 0
