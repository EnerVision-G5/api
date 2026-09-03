"""Accès asynchrone à PostgreSQL / TimescaleDB (SQLAlchemy 2 + asyncpg)."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Moteur asynchrone partagé, créé au premier accès.

    La création est différée pour que le simple import de l'application (tests
    unitaires, export du contrat OpenAPI, job contract-drift) n'ouvre aucun
    pool de connexions et ne réclame aucune base joignable.
    """
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Fabrique de sessions, mémoïsée pour partager le pool du moteur."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dépendance FastAPI : une session par requête, fermée à la sortie."""
    async with get_session_factory()() as session:
        yield session
