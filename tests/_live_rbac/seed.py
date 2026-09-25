"""Seed the test DB + redis(db13) with an RBAC scenario and mint MCP tokens.

Run with the live-replay env (see run.sh). Prints a JSON blob to stdout and
writes it to tests/_live_rbac/scenario.json.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "scenario.json"

MEMBER_TOKEN = "live-member-token-" + uuid.uuid4().hex
OWNER_TOKEN = "live-owner-token-" + uuid.uuid4().hex


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


async def main():
    import os

    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.models  # noqa: F401  (register all tables)
    from app.db.database import Base
    from app.models.bq_connection import BQConnection
    from app.models.connection import OAuthConnection
    from app.models.mcp_session import MCPSession
    from app.models.project import Project, ProjectMember
    from app.models.role import MemberRole, Role
    from app.models.user import User

    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        a_owner = User(email="live-a-owner@ex.com", display_name="A Owner")
        a_member = User(email="live-a-member@ex.com", display_name="A Member")
        b_owner = User(email="live-b-owner@ex.com", display_name="B Owner")
        db.add_all([a_owner, a_member, b_owner])
        await db.flush()

        proj_a = Project(name="Alpha", slug="alpha", owner_id=a_owner.id, rbac_enabled=True)
        proj_b = Project(name="Bravo", slug="bravo", owner_id=b_owner.id, rbac_enabled=True)
        db.add_all([proj_a, proj_b])
        await db.flush()

        pm_a_owner = ProjectMember(project_id=proj_a.id, user_id=a_owner.id, role="owner")
        pm_a_member = ProjectMember(project_id=proj_a.id, user_id=a_member.id, role="member")
        pm_b_owner = ProjectMember(project_id=proj_b.id, user_id=b_owner.id, role="owner")
        db.add_all([pm_a_owner, pm_a_member, pm_b_owner])
        await db.flush()

        role = Role(
            project_id=proj_a.id,
            name="Analyst (GA4 read)",
            permissions={"tools": {"analytics": ["read"]}, "providers": ["ga4"]},
            created_by=a_owner.id,
        )
        db.add(role)
        await db.flush()
        db.add(MemberRole(project_member_id=pm_a_member.id, role_id=role.id))

        # meta conn in A, BQ conn in B (for the cross-tenant presence test)
        db.add(
            OAuthConnection(
                project_id=proj_a.id,
                user_id=a_owner.id,
                provider="meta",
                google_email="ads@meta.test",
                access_token_encrypted="x",
                refresh_token_encrypted="x",
            )
        )
        db.add(
            BQConnection(
                fluxito_project_id=proj_b.id,
                user_id=b_owner.id,
                display_name="B warehouse",
                project_id="gcp-bravo",
                service_account_encrypted="x",
            )
        )

        exp = datetime.utcnow() + timedelta(hours=2)
        db.add(
            MCPSession(
                user_id=a_member.id,
                access_token_hash=_h(MEMBER_TOKEN),
                access_token_expires_at=exp,
                client_id="live-test",
                is_revoked=False,
            )
        )
        db.add(
            MCPSession(
                user_id=a_owner.id,
                access_token_hash=_h(OWNER_TOKEN),
                access_token_expires_at=exp,
                client_id="live-test",
                is_revoked=False,
            )
        )
        await db.commit()

        scenario = {
            "member_token": MEMBER_TOKEN,
            "owner_token": OWNER_TOKEN,
            "a_member": str(a_member.id),
            "a_owner": str(a_owner.id),
            "proj_a": str(proj_a.id),
            "proj_b": str(proj_b.id),
        }

    # Pre-set the member's active project in redis(db13) so tool calls scope to A.
    r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    await r.set(f"mcp:active_project:{scenario['a_member']}", scenario["proj_a"])
    await r.set(f"mcp:active_project:{scenario['a_owner']}", scenario["proj_a"])
    await r.aclose()

    await engine.dispose()
    OUT.write_text(json.dumps(scenario, indent=2))
    print(json.dumps(scenario))


asyncio.run(main())
