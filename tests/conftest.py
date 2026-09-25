"""
Test configuration — sets env vars before any app import, provides DB + Redis fixtures.
"""

import base64
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Project root on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env so TEST_DATABASE_URL (and other vars) are available
load_dotenv(_PROJECT_ROOT / ".env", override=False)

# Test env vars — MUST be set before any app.* import
_TEST_ENV = {
    "APP_ENV": "test",
    "APP_SECRET_KEY": "test-secret-key-must-be-at-least-32-chars-long!!",
    "APP_BASE_URL": "http://testserver",
    "DATABASE_URL": os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/fluxito_test",
    ),
    "REDIS_URL": "redis://localhost:6379/15",
    "TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"0" * 32).decode(),
    "GOOGLE_IDENTITY_REDIRECT_URI": "http://testserver/auth/google/identity/callback",
    "GOOGLE_DATA_REDIRECT_URI": "http://testserver/auth/google/data/callback",
    "GOOGLE_SIGNIN_REDIRECT_URI": "http://testserver/auth/google/signin/callback",
    "MCP_ALLOWED_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback,http://testserver/callback",
}
# Force-set all test env vars (overrides .env / shell exports so tests
# never accidentally hit the real database).
for _k, _v in _TEST_ENV.items():
    os.environ[_k] = _v


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def fake_redis():
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def _postgres_reachable() -> bool:
    """Pure-sync TCP check — no async, no SQLAlchemy, no event loop issues."""
    import socket
    import urllib.parse

    from app.config import settings as _s

    try:
        parsed = urllib.parse.urlparse(_s.DATABASE_URL.replace("+asyncpg", ""))
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except Exception:
        return False


_PG_AVAILABLE: bool | None = None


def _is_pg_available() -> bool:
    global _PG_AVAILABLE
    if _PG_AVAILABLE is None:
        _PG_AVAILABLE = _postgres_reachable()
    return _PG_AVAILABLE


@pytest.fixture
async def db_engine():
    if not _is_pg_available():
        pytest.skip("Postgres not reachable — run `docker compose up postgres`")

    from sqlalchemy.ext.asyncio import create_async_engine

    import app.models  # noqa: F401
    from app.db.database import Base

    # Create a dedicated test engine from the overridden DATABASE_URL
    # (the module-level engine in database.py may point to the real DB).
    test_engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)

    try:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await test_engine.dispose()
        pytest.skip(f"DB not usable ({exc.__class__.__name__}): create it with `createdb fluxito_test`")

    try:
        yield test_engine
    finally:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        finally:
            await test_engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    # Bind to the test engine, not the module-level one
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture(autouse=True)
def _reset_request_context():
    """Start every test with no MCP user/project context and a cold account-status cache.

    Tool-level RBAC reads these context vars; a value leaked from a previous test
    (e.g. a user with no project) would otherwise deny unrelated tool calls.
    """
    import app.app_state as _state
    from app.auth import account_status

    tokens = [_state.current_user_ctx.set(None), _state.current_project_ctx.set(None)]
    account_status.clear()
    yield
    for var, tok in zip((_state.current_user_ctx, _state.current_project_ctx), tokens, strict=True):
        try:
            var.reset(tok)
        except ValueError:
            pass
    account_status.clear()
