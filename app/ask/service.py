"""Persistence for chat conversations + messages."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update

from app import app_state
from app.ask.providers.base import LLMMessage, blocks_from_json, blocks_to_json
from app.models.conversation import ChatMessage, Conversation


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ConversationService:
    async def create(
        self, *, project_id: uuid.UUID, user_id: uuid.UUID, model: str, title: str | None = None
    ) -> Conversation:
        async with app_state.db_session_factory() as db:
            conv = Conversation(project_id=project_id, user_id=user_id, model=model, title=title)
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            return conv

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with app_state.db_session_factory() as db:
            return (
                await db.execute(select(Conversation).where(Conversation.id == conversation_id))
            ).scalar_one_or_none()

    async def list_for(
        self, *, project_id: uuid.UUID, user_id: uuid.UUID, query: str | None = None, limit: int = 50
    ) -> list[Conversation]:
        stmt = select(Conversation).where(
            Conversation.project_id == project_id, Conversation.user_id == user_id
        )
        q = (query or "").strip()
        if q:
            stmt = stmt.where(Conversation.title.ilike(f"%{_like_escape(q)}%", escape="\\"))
        stmt = stmt.order_by(Conversation.last_message_at.desc()).limit(limit)
        async with app_state.db_session_factory() as db:
            return list((await db.execute(stmt)).scalars().all())

    async def append(
        self, conversation_id: uuid.UUID, message: LLMMessage, *, token_usage: dict | None = None
    ) -> uuid.UUID:
        """Persist one turn; returns the new ChatMessage id."""
        async with app_state.db_session_factory() as db:
            next_seq = (
                await db.execute(
                    select(func.coalesce(func.max(ChatMessage.seq), -1) + 1).where(
                        ChatMessage.conversation_id == conversation_id
                    )
                )
            ).scalar_one()
            row = ChatMessage(
                conversation_id=conversation_id,
                role=message.role,
                seq=next_seq,
                content=blocks_to_json(message.content),
                token_usage=token_usage,
            )
            db.add(row)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(last_message_at=func.now())
            )
            await db.commit()
            await db.refresh(row)
            return row.id

    async def load_history(self, conversation_id: uuid.UUID) -> list[LLMMessage]:
        return [m for _, m, _ in await self.load_messages(conversation_id)]

    async def load_messages(
        self, conversation_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, LLMMessage, dict | None]]:
        """(row id, message, token_usage) for every stored turn, in order."""
        async with app_state.db_session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.conversation_id == conversation_id)
                        .order_by(ChatMessage.seq.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [
                (r.id, LLMMessage(role=r.role, content=blocks_from_json(r.content)), r.token_usage)
                for r in rows
            ]

    async def set_title(self, conversation_id: uuid.UUID, title: str) -> None:
        async with app_state.db_session_factory() as db:
            await db.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(title=title[:200])
            )
            await db.commit()

    async def delete(self, conversation_id: uuid.UUID) -> None:
        async with app_state.db_session_factory() as db:
            await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id))
            await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await db.commit()
