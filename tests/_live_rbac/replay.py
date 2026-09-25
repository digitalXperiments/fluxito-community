"""Drive the live /mcp endpoint with the official MCP client as a restricted
member, over real HTTP. Asserts RBAC enforcement end-to-end."""

import asyncio
import json
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8099/mcp"
SCN = json.loads((Path(__file__).parent / "scenario.json").read_text())

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


def _text(call_result):
    """Flatten a CallToolResult to a string for inspection."""
    out = []
    for c in call_result.content:
        out.append(getattr(c, "text", str(c)))
    return " ".join(out)


async def run(token, label):
    print(f"\n=== MCP session as {label} ===")
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) list_my_projects — must see ONLY Alpha (cross-project isolation)
            r = await session.call_tool("list_my_projects", {})
            txt = _text(r)
            check("list_my_projects shows Alpha", "alpha" in txt.lower() or "Alpha" in txt)
            check(
                "list_my_projects HIDES Bravo (no cross-tenant project)",
                "bravo" not in txt.lower(),
                detail=txt[:160],
            )

            # 2) tools/list — filtered by role
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            check("tools/list includes analytics_read (granted)", "analytics_read" in names)
            check(
                "tools/list EXCLUDES marketing_read (ungranted)",
                "marketing_read" not in names,
                detail=f"{len(names)} tools visible",
            )
            check("tools/list EXCLUDES warehouse_read (ungranted)", "warehouse_read" not in names)

            # 3) backstop: ungranted tool called directly → permission_denied
            r = await session.call_tool("marketing_read", {"action": "list_accounts"})
            txt = _text(r).lower()
            check(
                "backstop DENIES marketing_read",
                "permission_denied" in txt or "does not grant" in txt,
                detail=_text(r)[:160],
            )

            r = await session.call_tool("warehouse_read", {"action": "list_connections"})
            txt = _text(r).lower()
            check(
                "backstop DENIES warehouse_read",
                "permission_denied" in txt or "does not grant" in txt,
                detail=_text(r)[:160],
            )

            # 4) granted tool passes RBAC (not permission_denied; connection_missing is fine)
            r = await session.call_tool("analytics_read", {"action": "list_properties"})
            txt = _text(r).lower()
            check(
                "analytics_read PASSES RBAC (not denied)",
                "permission_denied" not in txt and "does not grant" not in txt,
                detail=_text(r)[:160],
            )


async def main():
    await run(SCN["member_token"], "restricted member")
    print(f"\n{sum(results)}/{len(results)} live checks passed")
    raise SystemExit(0 if all(results) else 1)


asyncio.run(main())
