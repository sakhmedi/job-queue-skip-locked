"""Весь доступ к таблице `jobs`.

Главное правило файла: каждое изменение состояния — ОДИН SQL-стейтмент, в котором
условие перехода стоит в WHERE. База сама гарантирует атомарность: два конкурентных
UPDATE одной строки выполняются по очереди, и второй заново проверяет WHERE на уже
изменённой строке. Никаких «прочитать в Python → решить → записать».

Каждая функция — законченная единица работы: сама делает commit, чтобы блокировки
строк держались как можно меньше.
"""

import uuid
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus


class JobNotFoundError(Exception):
    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"job {job_id} not found")


class JobConflictError(Exception):
    """Запрошенный переход статуса недопустим (→ HTTP 409)."""


async def create_job(session: AsyncSession, prompt: str, params: dict[str, Any]) -> Job:
    stmt = insert(Job).values(prompt=prompt, params=params).returning(Job)
    job = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return job


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    # populate_existing: перечитать из БД, даже если объект уже есть в сессии.
    return await session.get(Job, job_id, populate_existing=True)


# Подзапрос находит самое старое queued-задание и блокирует его строку.
# SKIP LOCKED: строки, уже заблокированные другими транзакциями (их прямо сейчас
# забирают другие воркеры), не ждём, а пропускаем. Внешний UPDATE в той же транзакции
# переводит задание в running. Подробности — в README.
_CLAIM_SQL = text("""
    UPDATE jobs
    SET status = 'running',
        worker_id = :worker_id,
        started_at = now(),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued'
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING *
""")


async def claim_job(session: AsyncSession, worker_id: str) -> Job | None:
    """Атомарно забрать одно самое старое queued-задание. None — очередь пуста."""
    stmt = select(Job).from_statement(_CLAIM_SQL).execution_options(populate_existing=True)
    job = (await session.execute(stmt, {"worker_id": worker_id})).scalar_one_or_none()
    await session.commit()  # commit снимает блокировку строки
    return job


# Переход running → succeeded/failed. Проверки «статус running» и «задание принадлежит
# этому воркеру» стоят прямо в WHERE, поэтому опоздавший или чужой воркер
# просто не найдёт строку для обновления.
_FINISH_SQL = text("""
    UPDATE jobs
    SET status = :new_status,
        result = :result,
        error = :error,
        finished_at = now(),
        updated_at = now()
    WHERE id = :job_id
      AND status = 'running'
      AND worker_id = :worker_id
    RETURNING *
""")


async def _finish_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    worker_id: str,
    *,
    new_status: JobStatus,
    result: str | None = None,
    error: str | None = None,
) -> Job:
    stmt = select(Job).from_statement(_FINISH_SQL).execution_options(populate_existing=True)
    params = {
        "job_id": job_id,
        "worker_id": worker_id,
        "new_status": new_status.value,
        "result": result,
        "error": error,
    }
    job = (await session.execute(stmt, params)).scalar_one_or_none()
    await session.commit()
    if job is not None:
        return job

    # UPDATE ничего не обновил. Решение уже принято атомарно, а этот SELECT нужен только
    # для понятного ответа: такого задания нет (404) или переход запрещён (409).
    current = await get_job(session, job_id)
    await session.commit()
    if current is None:
        raise JobNotFoundError(job_id)
    if current.status != JobStatus.RUNNING:
        raise JobConflictError(
            f"job {job_id} is '{current.status}', only 'running' jobs can be finished"
        )
    raise JobConflictError(
        f"job {job_id} is held by worker '{current.worker_id}', not '{worker_id}'"
    )


async def complete_job(
    session: AsyncSession, job_id: uuid.UUID, worker_id: str, result: str
) -> Job:
    return await _finish_job(
        session, job_id, worker_id, new_status=JobStatus.SUCCEEDED, result=result
    )


async def fail_job(session: AsyncSession, job_id: uuid.UUID, worker_id: str, error: str) -> Job:
    return await _finish_job(session, job_id, worker_id, new_status=JobStatus.FAILED, error=error)
