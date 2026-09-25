"""Project invitations: create, look up, accept, decline, revoke.

Nothing is granted until acceptance. Accepting requires the signed-in account's
email to match the invite (or, for a brand-new person, an account created from
the one-time link — the token vouches for the address, as the old temporary
password did).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

import app.app_state as app_state
from app.models.project import Project, ProjectMember
from app.models.project_invite import (
    INVITE_ACCEPTED,
    INVITE_DECLINED,
    INVITE_PENDING,
    INVITE_REVOKED,
    ProjectInvite,
)

INVITE_TTL = timedelta(days=14)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_url(token: str) -> str:
    from app.config import settings

    return f"{(settings.APP_BASE_URL or '').rstrip('/')}/invite/{token}"


def _is_open(inv: ProjectInvite) -> bool:
    return inv.status == INVITE_PENDING and inv.expires_at > datetime.utcnow()


async def create_invite(
    *, project_id: uuid.UUID, email: str, role: str, invited_by: uuid.UUID
) -> tuple[ProjectInvite, str]:
    """Create (or re-issue) the pending invite for ``email``. Returns (invite, plaintext token).

    Re-inviting the same email revokes the earlier pending invite so only the
    newest link works.
    """
    email = email.strip().lower()
    token = secrets.token_urlsafe(32)
    async with app_state.db_session_factory() as db:
        for old in (
            (
                await db.execute(
                    select(ProjectInvite).where(
                        ProjectInvite.project_id == project_id,
                        ProjectInvite.email == email,
                        ProjectInvite.status == INVITE_PENDING,
                    )
                )
            )
            .scalars()
            .all()
        ):
            old.status = INVITE_REVOKED
            old.responded_at = datetime.utcnow()
        inv = ProjectInvite(
            project_id=project_id,
            email=email,
            role=role,
            token_hash=_hash(token),
            status=INVITE_PENDING,
            invited_by=invited_by,
            expires_at=datetime.utcnow() + INVITE_TTL,
        )
        db.add(inv)
        await db.commit()
        await db.refresh(inv)
    return inv, token


async def get_by_token(token: str) -> ProjectInvite | None:
    if not token:
        return None
    async with app_state.db_session_factory() as db:
        return (
            await db.execute(select(ProjectInvite).where(ProjectInvite.token_hash == _hash(token)))
        ).scalar_one_or_none()


async def has_open_invite(email: str) -> bool:
    """True when ``email`` holds any usable invite (lets invitees sign up on invite-only installs)."""
    async with app_state.db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(ProjectInvite).where(
                        ProjectInvite.email == (email or "").strip().lower(),
                        ProjectInvite.status == INVITE_PENDING,
                    )
                )
            )
            .scalars()
            .all()
        )
    return any(_is_open(r) for r in rows)


async def pending_for_email(email: str) -> list[dict[str, Any]]:
    """Open invites addressed to ``email``, with project and inviter names (for the in-app banner)."""
    from app.models.user import User

    async with app_state.db_session_factory() as db:
        rows = (
            await db.execute(
                select(ProjectInvite, Project.name, User.display_name, User.email)
                .join(Project, Project.id == ProjectInvite.project_id)
                .outerjoin(User, User.id == ProjectInvite.invited_by)
                .where(
                    ProjectInvite.email == (email or "").strip().lower(),
                    ProjectInvite.status == INVITE_PENDING,
                    Project.is_active.is_(True),
                )
                .order_by(ProjectInvite.created_at.desc())
            )
        ).all()
    return [
        {
            "id": str(inv.id),
            "project_name": pname,
            "role": inv.role,
            "invited_by": iname or (iemail.split("@")[0] if iemail else "Someone"),
        }
        for inv, pname, iname, iemail in rows
        if _is_open(inv)
    ]


async def list_for_project(project_id: uuid.UUID) -> list[dict[str, Any]]:
    async with app_state.db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(ProjectInvite)
                    .where(ProjectInvite.project_id == project_id, ProjectInvite.status == INVITE_PENDING)
                    .order_by(ProjectInvite.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(r.id),
            "email": r.email,
            "role": r.role,
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "expired": not _is_open(r),
        }
        for r in rows
    ]


async def accept(invite_id: uuid.UUID, *, user_id: str, user_email: str) -> tuple[bool, str]:
    """Turn an open invite into an active membership for the signed-in user.

    Returns (ok, message). The account's email must be the invited address.
    """
    async with app_state.db_session_factory() as db:
        inv = await db.get(ProjectInvite, invite_id)
        if inv is None or not _is_open(inv):
            return False, "This invitation is no longer valid."
        if (user_email or "").strip().lower() != inv.email:
            return False, "This invitation was sent to a different email address."
        uid = uuid.UUID(str(user_id))
        member = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == inv.project_id, ProjectMember.user_id == uid
                )
            )
        ).scalar_one_or_none()
        now = datetime.utcnow()
        if member is None:
            db.add(
                ProjectMember(
                    project_id=inv.project_id,
                    user_id=uid,
                    role=inv.role,
                    invited_by=inv.invited_by,
                    invited_at=inv.created_at,
                    joined_at=now,
                )
            )
        elif not member.is_active:
            member.is_active = True
            member.role = inv.role
            member.invited_by = inv.invited_by
            member.invited_at = inv.created_at
            member.joined_at = now
        inv.status = INVITE_ACCEPTED
        inv.responded_at = now
        project_id = str(inv.project_id)
        await db.commit()

    from app.auth.mcp_session_manager import invalidate_project_context_cache, invalidate_user_context_cache
    from app.auth.permissions import invalidate_permissions_cache

    await invalidate_user_context_cache(str(user_id))
    await invalidate_permissions_cache(str(user_id), project_id)
    await invalidate_project_context_cache(project_id)
    return True, project_id


async def decline(invite_id: uuid.UUID, *, user_email: str) -> bool:
    async with app_state.db_session_factory() as db:
        inv = await db.get(ProjectInvite, invite_id)
        if inv is None or inv.status != INVITE_PENDING or (user_email or "").strip().lower() != inv.email:
            return False
        inv.status = INVITE_DECLINED
        inv.responded_at = datetime.utcnow()
        await db.commit()
    return True


async def revoke(invite_id: uuid.UUID, *, project_id: uuid.UUID) -> bool:
    async with app_state.db_session_factory() as db:
        inv = await db.get(ProjectInvite, invite_id)
        if inv is None or inv.project_id != project_id or inv.status != INVITE_PENDING:
            return False
        inv.status = INVITE_REVOKED
        inv.responded_at = datetime.utcnow()
        await db.commit()
    return True
