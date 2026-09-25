"""Community-edition boundary.

The public community edition is the MCP server, connections, Audit, Context and
a plain read-only Chat. Tracking plans, Implement, dashboards, templates,
automations and the Flux agent (avatar, memory, decision layer) live only in
the private Fluxito Cloud repo. These tests fail if any of them reappear here.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.tools.registry import register_all_tools

ROOT = Path(__file__).resolve().parent.parent

REMOVED_PATHS = [
    "app/services/tracking_plan",
    "app/services/implementation",
    "app/dashboards",
    "app/models/tracking_plan.py",
    "app/models/dashboard.py",
    "app/models/template.py",
    "app/models/automation.py",
    "app/models/flux_draft.py",
    "app/models/briefing_dismissal.py",
    "app/models/scheduled_report.py",
    "app/tools/tracking_plan_tools.py",
    "app/tools/dashboard_tools.py",
    "app/tools/template_tools.py",
    "app/tools/automation_tools.py",
    "app/tools/sdr_audit_helpers.py",
    "app/api/tracking_plan_routes.py",
    "app/api/implement_routes.py",
    "app/api/dashboard_routes.py",
    "app/api/template_routes.py",
    "app/api/automation_routes.py",
    "app/api/scheduled_report_routes.py",
    "app/ask/drafts.py",
    "app/static/js/flux/dock.js",
]

# Cloud-only subsystems that must never be added to the community edition.
CLOUD_ONLY_PATHS = [
    "app/memory",
    "app/decisioning",
    "app/sentinel",
    "app/flux",
    "app/static/js/flux/avatar.js",
]

REMOVED_TOOLS = {
    "tracking_plan",
    "tracking_plan_v2",
    "dashboard_read",
    "deploy_dashboard",
    "update_dashboard",
    "bind_dashboard",
    "delete_dashboard",
    "dashboard_manage_scopes",
    "dashboard_rotate_token",
    "deploy_knowledge",
    "automation_read",
    "automation_write",
}

# Names of the cloud-only decision/memory layer; none may appear in the code.
CLOUD_ONLY_MARKERS = ("typesafe", "jev_", "askjev", "flux_memory", "sentinel_")


def test_removed_features_are_absent():
    present = [p for p in REMOVED_PATHS + CLOUD_ONLY_PATHS if (ROOT / p).exists()]
    assert present == []


def test_removed_mcp_tools_are_not_registered():
    mcp = FastMCP(name="community-boundary")
    register_all_tools(mcp)
    names = set(mcp._tool_manager._tools)
    assert names & REMOVED_TOOLS == set()
    assert "get_knowledge" in names
    assert "run_audit" in names


def test_no_cloud_only_code_markers():
    hits = []
    for path in (ROOT / "app").rglob("*"):
        if path.suffix not in {".py", ".js", ".html", ".css", ".json"}:
            continue
        text = path.read_text(errors="ignore").lower()
        hits += [f"{path.relative_to(ROOT)}: {m}" for m in CLOUD_ONLY_MARKERS if m in text]
    assert hits == []


def test_single_baseline_migration():
    versions = sorted((ROOT / "app/db/migrations/versions").glob("*.py"))
    assert [v.name for v in versions] == ["001_community_baseline.py"]
    text = versions[0].read_text()
    assert "down_revision: Union[str, Sequence[str], None] = None" in text
    assert "'claude-ai'" in text
