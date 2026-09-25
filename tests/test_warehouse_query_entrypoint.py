"""
Regression: run_analysis warehouse revenue adapter import (FINDINGS S1 #6).

warehouse_query is a closure registered inside register_warehouse_tools(), so
`from app.tools.warehouse_tools import warehouse_query` always raised ImportError.
cross_platform_tools swallowed it, so the warehouse revenue ground-truth path
silently never ran. The fix publishes a module-level `warehouse_query_impl`.
"""

from __future__ import annotations

import app.tools.warehouse_tools as wt


def test_warehouse_query_impl_is_importable():
    # The exact symbol run_analysis's adapter now imports — must exist at module level.
    from app.tools.warehouse_tools import warehouse_query_impl

    assert callable(warehouse_query_impl)


async def test_impl_returns_clean_error_before_registration():
    saved = wt._WAREHOUSE_QUERY_FN
    wt._WAREHOUSE_QUERY_FN = None
    try:
        out = await wt.warehouse_query_impl(engine="bigquery", action="run_query", query="SELECT 1")
        assert out["error"] is True
        assert out["error_type"] == "not_registered"
    finally:
        wt._WAREHOUSE_QUERY_FN = saved


def test_register_publishes_warehouse_query():
    from mcp.server.fastmcp import FastMCP

    from app.tools.registry import register_all_tools

    mcp = FastMCP(name="test-wh-entrypoint")
    register_all_tools(mcp)
    # After registration the in-process handle is wired to the real closure.
    assert wt._WAREHOUSE_QUERY_FN is not None
    assert wt._WAREHOUSE_QUERY_FN.__name__ == "warehouse_query"
