# app/auth/permissions.py
"""RBAC permission vocabulary, effective-permission resolution, and caching.

Two axes per role: tool capabilities (domain x read/write) and connection
access (per provider). See the design spec for the full vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tool domains -> dispatcher tools (read vs write)
DOMAIN_TOOLS: dict[str, dict[str, set[str]]] = {
    "analytics": {"read": {"analytics_read"}, "write": {"analytics_write"}},
    "audience": {"read": {"audience_read"}, "write": {"audience_write"}},
    "tagmanager": {"read": {"tagmanager_read"}, "write": {"tagmanager_write"}},
    "marketing": {"read": {"marketing_read"}, "write": {"marketing_write"}},
    "seo": {"read": {"seo_read"}, "write": {"seo_write"}},
    "warehouse": {"read": {"warehouse_read", "warehouse_query"}, "write": set()},
    # Knowledge writes happen in the web UI (KPI library, business context).
    "knowledge": {"read": {"get_knowledge"}, "write": set()},
    # tag_rulebook / live_tag_test / save_audit_result are exposed BOTH as direct
    # tools and as run_audit actions. They were absent from this map, so
    # allows_tool() denied the direct tools to every non-`full` user while the
    # run_audit twins stayed open (FINDINGS S1 #10). They live in the analysis
    # domain; their write actions are gated per-action below (FINDINGS S1 #9).
    "analysis": {
        "read": {"run_analysis", "run_audit", "tag_rulebook", "live_tag_test", "save_audit_result"},
        "write": {"run_audit", "tag_rulebook", "live_tag_test", "save_audit_result"},
    },
}

# Analysis-domain tools whose level depends on the action.
# Covers the direct tools AND their run_audit twins. Any action NOT listed as a
# write is treated as a read, so reads stay available under analysis:read while
# mutations require analysis:write — even when invoked through run_audit
# (FINDINGS S1 #9). Keep in sync with the routing tables in app/tools/unified.py.
_ANALYSIS_ACTION_TOOLS: set[str] = {
    "run_audit",
    "tag_rulebook",
    "live_tag_test",
    "save_audit_result",
}
_ACTION_WRITE_TOOLS: dict[str, set[str]] = {
    "tag_rulebook": {"save_custom_rule", "delete_custom_rule"},
    "live_tag_test": {"start_session", "finish_session", "save_test_plan"},
    "save_audit_result": {"save"},
    "run_audit": {
        "tag_save_custom_rule",
        "tag_delete_custom_rule",
        "live_tag_save_plan",
        "live_tag_start_session",
        "live_tag_finish_session",
        "save_audit_result",
    },
}


def is_write_call(tool_name: str, action: str | None = None) -> bool:
    """True when a tool call changes something (needs an MCP token with ``write``).

    ``run_script`` / ``generic_tool_read`` are containers: each inner call is
    checked on its own. Tools absent from the vocabulary count as reads.
    """
    if tool_name in ALWAYS_ON_TOOLS or tool_name in ("run_script", "generic_tool_read"):
        return False
    if tool_name == "generic_tool_write":
        return True
    if tool_name in _ANALYSIS_ACTION_TOOLS:
        return action in _ACTION_WRITE_TOOLS.get(tool_name, set())
    req = _TOOL_TO_REQ.get(tool_name)
    return bool(req and req[1] == "write")


_ADVANCED_TOOLS: dict[str, str] = {
    "run_script": "scripting",
    "generic_tool_read": "generic_tools",
    "generic_tool_write": "generic_tools",
}

ALWAYS_ON_TOOLS: set[str] = {"get_session_context", "list_my_projects", "set_active_project"}

PROVIDERS: list[str] = [
    "ga4",
    "gtm",
    "google_ads",
    "data_manager",
    "gsc",
    "bing",
    "meta",
    "tiktok",
    "snap",
    "linkedin",
    "pinterest",
    "x",
    "reddit",
    "apple",
    "amplitude",
    "mixpanel",
    "posthog",
    "braze",
    "moengage",
    "adobe_analytics",
    "adobe_launch",
    "adobe_marketo",
    "bigquery",
    "redshift",
    "snowflake",
]

_TOOL_TO_REQ: dict[str, tuple[str, str]] = {}
for _domain, _levels in DOMAIN_TOOLS.items():
    for _level in ("read", "write"):
        for _tool in _levels[_level]:
            _TOOL_TO_REQ.setdefault(_tool, (_domain, _level))


@dataclass
class EffectivePermissions:
    """Resolved permissions for one (user, project)."""

    full: bool = False
    tools: dict[str, set[str]] = field(default_factory=dict)
    providers: set[str] = field(default_factory=set)
    advanced: set[str] = field(default_factory=set)

    def allows_tool(self, tool_name: str, action: str | None = None) -> bool:
        if tool_name in ALWAYS_ON_TOOLS:
            return True
        if self.full:
            return True
        if tool_name in _ADVANCED_TOOLS:
            return _ADVANCED_TOOLS[tool_name] in self.advanced
        if tool_name in _ANALYSIS_ACTION_TOOLS:
            # Read vs write decided by the action (covers run_audit twins too).
            write_actions = _ACTION_WRITE_TOOLS.get(tool_name, set())
            level = "write" if action in write_actions else "read"
            return level in self.tools.get("analysis", set())
        req = _TOOL_TO_REQ.get(tool_name)
        if req is None:
            return False
        domain, level = req
        return level in self.tools.get(domain, set())

    def allows_provider(self, provider: str) -> bool:
        if self.full:
            return True
        return provider in self.providers


# ---------------------------------------------------------------------------
# Normalization, union helpers, Redis cache, and async resolver
# ---------------------------------------------------------------------------
import json
import logging

logger = logging.getLogger(__name__)

_PERMS_CACHE_TTL = 120  # seconds; mirrors mcp_session_manager UserContext TTL


class PermissionValidationError(ValueError):
    """Raised when a permissions document references unknown domains/providers/levels."""


def normalize_permissions(doc: dict) -> dict:
    """Validate + normalize a role permissions doc. Write implies read."""
    doc = doc or {}
    out: dict = {"tools": {}, "providers": [], "advanced": {}}

    tools = doc.get("tools", {}) or {}
    for domain, levels in tools.items():
        if domain not in DOMAIN_TOOLS:
            raise PermissionValidationError(f"unknown tool domain: {domain}")
        lv = set(levels or [])
        if not lv <= {"read", "write"}:
            raise PermissionValidationError(f"invalid levels for {domain}: {levels}")
        if "write" in lv:
            lv.add("read")
        if lv:
            out["tools"][domain] = sorted(lv)

    for prov in doc.get("providers", []) or []:
        if prov not in PROVIDERS:
            raise PermissionValidationError(f"unknown provider: {prov}")
        out["providers"].append(prov)

    adv = doc.get("advanced", {}) or {}
    for key in ("scripting", "generic_tools"):
        out["advanced"][key] = bool(adv.get(key, False))
    return out


def _union_role_docs(docs: list[dict]) -> EffectivePermissions:
    eff = EffectivePermissions(full=False)
    for doc in docs:
        for domain, levels in (doc.get("tools", {}) or {}).items():
            eff.tools.setdefault(domain, set()).update(levels or [])
        eff.providers.update(doc.get("providers", []) or [])
        adv = doc.get("advanced", {}) or {}
        for key, on in adv.items():
            if on:
                eff.advanced.add(key)
    return eff


def _redis_client():
    """Return the app's async Redis client, or None if unavailable."""
    try:
        import app.app_state as state

        return getattr(state, "redis_client", None)
    except Exception:
        return None


def _cache_key(user_id: str, project_id: str) -> str:
    return f"perms:{user_id}:{project_id}"


async def _cache_get(user_id: str, project_id: str):
    """Return a cached EffectivePermissions or None. Degrades gracefully without Redis."""
    try:
        r = _redis_client()
        if r is None:
            return None
        raw = await r.get(_cache_key(user_id, project_id))
        if not raw:
            return None
        d = json.loads(raw)
        return EffectivePermissions(
            full=d["full"],
            tools={k: set(v) for k, v in d.get("tools", {}).items()},
            providers=set(d.get("providers", [])),
            advanced=set(d.get("advanced", [])),
        )
    except Exception:
        return None


async def _cache_set(user_id: str, project_id: str, eff: EffectivePermissions) -> None:
    """Cache EffectivePermissions. No-op if Redis is unavailable."""
    try:
        r = _redis_client()
        if r is None:
            return
        payload = json.dumps(
            {
                "full": eff.full,
                "tools": {k: sorted(v) for k, v in eff.tools.items()},
                "providers": sorted(eff.providers),
                "advanced": sorted(eff.advanced),
            }
        )
        await r.setex(_cache_key(user_id, project_id), _PERMS_CACHE_TTL, payload)
    except Exception:
        pass


async def invalidate_permissions_cache(user_id: str, project_id: str) -> None:
    """Delete the cached EffectivePermissions for (user, project)."""
    try:
        r = _redis_client()
        if r is not None:
            await r.delete(_cache_key(user_id, project_id))
    except Exception:
        pass


async def resolve_effective_permissions(user_id: str, project_id: str) -> EffectivePermissions:
    """Resolve (user, project) -> EffectivePermissions.

    Rules:
    - owner/admin role  OR  rbac_enabled=False  →  full=True
    - member with RBAC on  →  union of their active assigned roles' permissions
    - non-member or unknown project  →  empty (deny-all)

    Result is cached in Redis for _PERMS_CACHE_TTL seconds; degrades gracefully
    when Redis is absent (e.g. unit tests monkeypatch _cache_get/_cache_set).
    """
    import uuid as _uuid

    from sqlalchemy import select

    import app.app_state as state
    from app.models.project import ROLE_ADMIN, ROLE_OWNER, Project, ProjectMember
    from app.models.role import MemberRole, Role

    cached = await _cache_get(user_id, project_id)
    if cached is not None:
        return cached

    async with state.db_session_factory() as db:
        proj = (
            await db.execute(select(Project).where(Project.id == _uuid.UUID(project_id)))
        ).scalar_one_or_none()
        if proj is None:
            eff = EffectivePermissions(full=False)
            await _cache_set(user_id, project_id, eff)
            return eff

        pm = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == proj.id,
                    ProjectMember.user_id == _uuid.UUID(user_id),
                    ProjectMember.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()

        if pm is None:
            eff = EffectivePermissions(full=False)
        elif pm.role in (ROLE_OWNER, ROLE_ADMIN) or not proj.rbac_enabled:
            eff = EffectivePermissions(full=True)
        else:
            docs = (
                (
                    await db.execute(
                        select(Role.permissions)
                        .join(MemberRole, MemberRole.role_id == Role.id)
                        .where(MemberRole.project_member_id == pm.id, Role.is_active.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            eff = _union_role_docs(list(docs))

    await _cache_set(user_id, project_id, eff)
    return eff
