from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,  # Never echo in production — use logging config for debug
    pool_pre_ping=True,
    pool_size=30,  # base persistent connections — raised so bursty traffic
    # doesn't immediately spill into `max_overflow` and pay connection setup
    max_overflow=20,  # burst to 50 total under load
    pool_recycle=900,  # 15 min — recycle sooner than typical idle timeouts
    pool_timeout=5,  # fail fast: blocking > 5s usually means true exhaustion
)


def pool_stats() -> dict:
    """Return current SQLAlchemy pool metrics for /api/health."""
    p = engine.pool
    return {
        "size": p.size(),
        "checked_in": p.checkedin(),
        "checked_out": p.checkedout(),
        "overflow": p.overflow(),
    }


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
