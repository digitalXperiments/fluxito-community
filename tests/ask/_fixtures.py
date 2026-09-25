"""Shared seed helpers for the chat tests."""

import uuid


async def seed_user_project(db_session_factory, tag=None):
    from app.models.project import Project
    from app.models.user import User

    tag = tag or uuid.uuid4().hex[:8]
    async with db_session_factory() as db:
        u = User(email=f"chat-{tag}@example.com")
        db.add(u)
        await db.flush()
        p = Project(name=f"Chat {tag}", slug=f"chat-{tag}", owner_id=u.id)
        db.add(p)
        await db.flush()
        uid, pid = u.id, p.id
        await db.commit()
    return uid, pid
