"""Общие фикстуры. Тесты идут на настоящем PostgreSQL: SKIP LOCKED на SQLite не проверить.

База берётся из TEST_DATABASE_URL (в docker compose — `jobs_test` в сервисе db),
создаётся при необходимости и мигрируется Alembic'ом, то есть тестируется и миграция.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/jobs_test"
)
WORKER_TOKEN = "test-worker-token"

# Переменные окружения выставляем ДО импорта app.*: настройки читаются один раз.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["WORKER_TOKEN"] = WORKER_TOKEN
os.environ["REAPER_ENABLED"] = "false"

import httpx  # noqa: E402

from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402

_db_name = make_url(TEST_DATABASE_URL).database or ""
# Защита от случайного запуска на рабочей базе: перед каждым тестом делаем TRUNCATE.
assert _db_name.endswith("_test"), f"refusing to run tests on database {_db_name!r}"


async def _create_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> None:
    asyncio.run(_create_test_database())
    # Миграцию запускаем так же, как в проде: `alembic upgrade head`.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий на чистой таблице.

    NullPool: каждая сессия открывает СВОЁ соединение с PostgreSQL. Для тестов гонок
    это важно: «два воркера» — это две разные транзакции, как в реальности.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE jobs"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP-клиент к приложению в памяти (без сети). Lifespan (reaper) не запускается."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}


@pytest.fixture
def create_job(client: httpx.AsyncClient) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Создать задание через API и вернуть JSON ответа."""

    async def _create(prompt: str = "test prompt", **params: Any) -> dict[str, Any]:
        resp = await client.post("/jobs", json={"prompt": prompt, "params": params})
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _create


@pytest.fixture
def claim(
    client: httpx.AsyncClient, worker_headers: dict[str, str]
) -> Callable[[str], Awaitable[httpx.Response]]:
    async def _claim(worker_id: str) -> httpx.Response:
        return await client.post(
            "/jobs/claim", json={"worker_id": worker_id}, headers=worker_headers
        )

    return _claim
