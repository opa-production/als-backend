from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _connect_args() -> dict[str, Any]:
    """
    asyncpg options that depend on what sits between us and Postgres.

    Behind a transaction-mode pooler, one server connection is shared between
    clients statement by statement. asyncpg caches prepared statements per
    connection, so the second identical query can arrive on a connection that
    never prepared it — which surfaces as a baffling
    ``InvalidSQLStatementNameError`` under load and never in development.
    """
    if not settings.database_use_pgbouncer:
        return {}

    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }


def create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        # Recycle before a pooler or cloud provider drops an idle connection;
        # otherwise the first request after a quiet spell dies on a stale one.
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_args=_connect_args(),
    )


engine: AsyncEngine = create_engine()

#: ``expire_on_commit=False`` so a returned ORM object stays readable after the
#: session closes. Without it, serialising a response after commit triggers a
#: lazy refresh on a dead session — which in async code is an error, not a
#: slow query.
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    One session per request, committed on success and rolled back on failure.

    A FastAPI dependency, so the transaction boundary is the request boundary.
    Handlers never commit: a handler that commits halfway leaves the request
    unable to fail cleanly.
    """
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Closes the pool on shutdown so containers exit instead of hanging."""
    await engine.dispose()
