"""Точка входа FastAPI: роутеры, обработчики ошибок, фоновый reaper."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, engine, get_session
from app.reaper import reaper_loop
from app.repository import JobConflictError, JobNotFoundError
from app.routers import jobs

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("app")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Код до `yield` выполняется при старте сервера, после — при остановке."""
    reaper_task = None
    if settings.reaper_enabled:
        # Аналог `go reaperLoop(ctx)`: задача крутится в том же event loop, что и запросы.
        reaper_task = asyncio.create_task(
            reaper_loop(SessionLocal, settings.reaper_interval_seconds), name="reaper"
        )
    yield
    if reaper_task is not None:
        reaper_task.cancel()  # аналог cancel() у context.Context
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
    await engine.dispose()


app = FastAPI(title="Job queue (SKIP LOCKED)", lifespan=lifespan)
app.include_router(jobs.router)


@app.exception_handler(JobNotFoundError)
async def _not_found(_request: Request, exc: JobNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(JobConflictError)
async def _conflict(_request: Request, exc: JobConflictError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> JSONResponse:
    """Liveness + доступность БД (используется healthcheck'ом docker compose)."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("health check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "db": "unavailable"},
        )
    return JSONResponse(content={"status": "ok", "db": "ok"})
