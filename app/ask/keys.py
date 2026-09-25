"""A user's personal chat model configuration (OpenAI-compatible endpoint).

One row per user in ``ai_provider_keys``: base URL, model and an optional
Fernet-encrypted API key. Keys are personal — never shared with a project.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select

from app import app_state
from app.models.conversation import AIProviderKey
from app.utils.encryption import decrypt_str, encrypt_str


@dataclass
class ChatConfig:
    """The full config, including the decrypted secret (server-side only)."""

    base_url: str
    model: str
    api_key: str | None


@dataclass
class ChatConfigInfo:
    """Public view of a config — never includes the secret."""

    base_url: str
    model: str
    has_key: bool
    key_hint: str | None  # e.g. "••••abcd"


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _hint(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return "••••" + api_key[-4:] if len(api_key) > 8 else "••••"


async def _row(db, user_id: uuid.UUID) -> AIProviderKey | None:
    return (
        await db.execute(select(AIProviderKey).where(AIProviderKey.user_id == user_id))
    ).scalar_one_or_none()


async def get_config(user_id: uuid.UUID) -> ChatConfig | None:
    async with app_state.db_session_factory() as db:
        row = await _row(db, user_id)
        if row is None:
            return None
        return ChatConfig(
            base_url=row.base_url,
            model=row.model,
            api_key=decrypt_str(row.api_key_encrypted) if row.api_key_encrypted else None,
        )


async def get_config_info(user_id: uuid.UUID) -> ChatConfigInfo | None:
    cfg = await get_config(user_id)
    if cfg is None:
        return None
    return ChatConfigInfo(
        base_url=cfg.base_url,
        model=cfg.model,
        has_key=bool(cfg.api_key),
        key_hint=_hint(cfg.api_key),
    )


async def resolve_api_key(user_id: uuid.UUID, base_url: str, api_key: str | None) -> str | None:
    """The key to use for *base_url*: the supplied one, else the stored key —
    but only when the stored config points at the same base URL. A saved
    secret is never handed to a different endpoint."""
    if api_key:
        return api_key
    stored = await get_config(user_id)
    if stored is not None and normalize_base_url(stored.base_url) == normalize_base_url(base_url):
        return stored.api_key
    return None


async def save_config(*, user_id: uuid.UUID, base_url: str, model: str, api_key: str | None) -> None:
    """Create or update the user's config.

    ``api_key`` None/empty keeps the stored key when the base URL is unchanged;
    changing the base URL without a new key clears the stored key.
    """
    base_url = normalize_base_url(base_url)
    async with app_state.db_session_factory() as db:
        row = await _row(db, user_id)
        if row is None:
            db.add(
                AIProviderKey(
                    user_id=user_id,
                    base_url=base_url,
                    model=model,
                    api_key_encrypted=encrypt_str(api_key) if api_key else None,
                )
            )
        else:
            if api_key:
                row.api_key_encrypted = encrypt_str(api_key)
            elif normalize_base_url(row.base_url) != base_url:
                row.api_key_encrypted = None
            row.base_url = base_url
            row.model = model
        await db.commit()


async def delete_config(user_id: uuid.UUID) -> None:
    async with app_state.db_session_factory() as db:
        await db.execute(delete(AIProviderKey).where(AIProviderKey.user_id == user_id))
        await db.commit()
