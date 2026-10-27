"""Подключение к PostgreSQL через SQLAlchemy 2.0 (async) + asyncpg."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

# Engine держит пул соединений. Сам по себе он ничего не открывает —
# соединение берётся из пула при первом запросе.
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)

# expire_on_commit=False: после commit() объекты остаются читаемыми
# (иначе обращение к job.status после коммита потребовало бы нового запроса).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна сессия (= одно соединение из пула) на HTTP-запрос."""
    async with SessionLocal() as session:
        yield session
