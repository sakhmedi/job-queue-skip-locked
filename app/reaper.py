"""Reaper: возвращает в очередь задания, чей воркер, судя по всему, умер.

Воркер мог упасть, потерять сеть или зависнуть — тогда задание навсегда осталось бы
в `running`. Reaper периодически находит такие задания и:
  * attempts < MAX_ATTEMPTS  → `queued` (его заберёт другой воркер);
  * attempts >= MAX_ATTEMPTS → `failed` с error="max attempts exceeded"
    («ядовитое» задание, которое, например, роняет воркер, не крутится вечно).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import JobStatus

logger = logging.getLogger(__name__)

# Один стейтмент: CTE `stale` выбирает и блокирует зависшие строки, UPDATE их меняет.
# SKIP LOCKED делает безопасным запуск нескольких экземпляров api: две копии reaper'а
# не обработают одну и ту же строку, а строку, которую в этот момент завершает воркер
# (complete держит блокировку), reaper пропустит до следующего прохода.
_REQUEUE_SQL = text("""
    WITH stale AS (
        SELECT id FROM jobs
        WHERE status = 'running' AND started_at < :cutoff
        ORDER BY started_at
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs AS j
    SET status = CASE WHEN j.attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
        error = CASE WHEN j.attempts >= :max_attempts
                     THEN 'max attempts exceeded' ELSE j.error END,
        finished_at = CASE WHEN j.attempts >= :max_attempts
                           THEN CAST(:now AS timestamptz) ELSE NULL END,
        worker_id = NULL,
        started_at = NULL,
        updated_at = CAST(:now AS timestamptz)
    FROM stale
    WHERE j.id = stale.id
    RETURNING j.id, j.status
""")


@dataclass
class ReapResult:
    requeued: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)


async def requeue_stale_jobs(
    session: AsyncSession,
    now: datetime,
    *,
    stale_after_seconds: int | None = None,
    max_attempts: int | None = None,
) -> ReapResult:
    """Один проход reaper'а. `now` передаётся явно, чтобы в тестах управлять временем."""
    settings = get_settings()
    if stale_after_seconds is None:
        stale_after_seconds = settings.stale_after_seconds
    if max_attempts is None:
        max_attempts = settings.max_attempts

    rows = await session.execute(
        _REQUEUE_SQL,
        {
            "cutoff": now - timedelta(seconds=stale_after_seconds),
            "max_attempts": max_attempts,
            "now": now,
        },
    )
    result = ReapResult()
    for job_id, status in rows:
        if status == JobStatus.FAILED:
            result.failed.append(job_id)
        else:
            result.requeued.append(job_id)
    await session.commit()
    return result


async def reaper_loop(session_factory: async_sessionmaker[AsyncSession], interval: float) -> None:
    """Фоновая задача api: вызывает requeue_stale_jobs раз в `interval` секунд."""
    logger.info("reaper started, interval=%ss", interval)
    while True:
        try:
            async with session_factory() as session:
                # Время берём у БД, а не у api: started_at тоже ставит БД (now()),
                # так расхождение часов между машинами ни на что не влияет.
                now = await session.scalar(text("SELECT now()"))
                result = await requeue_stale_jobs(session, now)
            if result.requeued or result.failed:
                logger.warning(
                    "reaper: requeued=%d failed=%d (requeued ids: %s, failed ids: %s)",
                    len(result.requeued),
                    len(result.failed),
                    [str(i) for i in result.requeued],
                    [str(i) for i in result.failed],
                )
        except Exception:
            # Ошибка одного прохода (например, БД ненадолго недоступна) не должна
            # убивать reaper. CancelledError не наследует Exception, поэтому
            # остановка задачи при shutdown здесь не перехватывается.
            logger.exception("reaper iteration failed")
        await asyncio.sleep(interval)
