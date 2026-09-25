# app/auth/web_guards.py
"""Reusable RBAC guards for server-rendered page routes."""

from fastapi import HTTPException


async def require_domain_permission(user_id: str, project_id: str, domain: str, level: str = "read") -> None:
    """Raise 403 unless the (user, project) has `level` on `domain`."""
    from app.auth import permissions as _perms

    if domain not in _perms.DOMAIN_TOOLS:
        raise HTTPException(status_code=500, detail=f"unknown domain {domain}")
    eff = await _perms.resolve_effective_permissions(user_id, project_id)
    if eff.full:
        return
    if level in eff.tools.get(domain, set()):
        return
    raise HTTPException(status_code=403, detail="You don't have access to this section.")


async def require_admin(user_project_role: str | None) -> None:
    """Raise 403 unless the caller is owner/admin in the active project."""
    from app.models.project import CAN_MANAGE_ROLES

    if user_project_role not in CAN_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required.")
