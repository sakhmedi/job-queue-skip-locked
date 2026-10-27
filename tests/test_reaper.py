"""Reaper: возврат зависших заданий и лимит попыток.

Вместо ожидания 10 минут сдвигаем started_at в прошлое и передаём `now` явно.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import text

from app import repository
from app.reaper import requeue_stale_jobs

STALE = 600  # секунд, как STALE_AFTER_SECONDS по умолчанию


async def _db_now(session):
    return await session.scalar(text("SELECT now()"))


async def _age(session, job_id, seconds):
    """Сделать вид, что задание запущено `seconds` секунд назад."""
    await session.execute(
        text("UPDATE jobs SET started_at = started_at - CAST(:delta AS interval) WHERE id = :id"),
        {"delta": timedelta(seconds=seconds), "id": job_id},
    )
    await session.commit()


async def _reap(session, max_attempts=3):
    return await requeue_stale_jobs(
        session, await _db_now(session), stale_after_seconds=STALE, max_attempts=max_attempts
    )


async def test_stale_running_job_is_requeued(session):
    job = await repository.create_job(session, "p", {})
    await repository.claim_job(session, "dead-worker")
    await _age(session, job.id, STALE + 1)

    result = await _reap(session)

    assert result.requeued == [job.id]
    assert result.failed == []
    job = await repository.get_job(session, job.id)
    assert job.status == "queued"
    assert job.worker_id is None
    assert job.started_at is None
    assert job.attempts == 1  # счётчик попыток не сбрасывается


async def test_fresh_running_job_is_untouched(session):
    job = await repository.create_job(session, "p", {})
    await repository.claim_job(session, "alive-worker")
    await _age(session, job.id, STALE // 2)  # 5 минут — ещё нормально

    result = await _reap(session)

    assert result.requeued == []
    job = await repository.get_job(session, job.id)
    assert job.status == "running"
    assert job.worker_id == "alive-worker"


async def test_reaper_ignores_queued_and_finished_jobs(session):
    done = await repository.create_job(session, "d", {})
    queued = await repository.create_job(session, "q", {})
    await repository.claim_job(session, "w1")  # FIFO: забирается `done`, созданное первым
    await repository.complete_job(session, done.id, "w1", "ok")

    later = (await _db_now(session)) + timedelta(days=1)
    await session.commit()
    result = await requeue_stale_jobs(session, later, stale_after_seconds=STALE, max_attempts=3)

    assert result.requeued == [] and result.failed == []
    assert (await repository.get_job(session, queued.id)).status == "queued"
    assert (await repository.get_job(session, done.id)).status == "succeeded"


async def test_requeued_job_can_be_claimed_again(session):
    job = await repository.create_job(session, "p", {})
    await repository.claim_job(session, "dead-worker")
    await _age(session, job.id, STALE + 1)
    await _reap(session)

    again = await repository.claim_job(session, "new-worker")

    assert again.id == job.id
    assert again.status == "running"
    assert again.worker_id == "new-worker"
    assert again.attempts == 2


async def test_max_attempts_exceeded_marks_failed(session):
    job = await repository.create_job(session, "poison", {})

    # Попытки 1 и 2 зависают → возврат в очередь
    for attempt in (1, 2):
        claimed = await repository.claim_job(session, f"w{attempt}")
        assert claimed.attempts == attempt
        await _age(session, job.id, STALE + 1)
        result = await _reap(session, max_attempts=3)
        assert result.requeued == [job.id]

    # Попытка 3 — последняя: после зависания задание проваливается окончательно
    claimed = await repository.claim_job(session, "w3")
    assert claimed.attempts == 3
    await _age(session, job.id, STALE + 1)
    result = await _reap(session, max_attempts=3)

    assert result.failed == [job.id]
    assert result.requeued == []
    job = await repository.get_job(session, job.id)
    assert job.status == "failed"
    assert job.error == "max attempts exceeded"
    assert job.finished_at is not None
    assert job.worker_id is None
    assert await repository.claim_job(session, "w4") is None  # больше не выдаётся


async def test_late_complete_from_dead_worker_is_409(session, client, worker_headers):
    job = await repository.create_job(session, "p", {})
    await repository.claim_job(session, "slow-worker")
    await _age(session, job.id, STALE + 1)
    await _reap(session)

    # Пока задание в очереди: старый воркер — 409
    resp = await client.post(
        f"/jobs/{job.id}/complete",
        json={"worker_id": "slow-worker", "result": "late"},
        headers=worker_headers,
    )
    assert resp.status_code == 409

    # Задание забрал новый воркер: старый всё ещё 409, новый — 200
    await repository.claim_job(session, "fresh-worker")
    resp = await client.post(
        f"/jobs/{job.id}/complete",
        json={"worker_id": "slow-worker", "result": "late"},
        headers=worker_headers,
    )
    assert resp.status_code == 409
    resp = await client.post(
        f"/jobs/{job.id}/complete",
        json={"worker_id": "fresh-worker", "result": "on time"},
        headers=worker_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "on time"


async def test_parallel_reapers_do_not_double_process(session_factory):
    """Несколько экземпляров api → несколько reaper'ов одновременно. Каждое задание — один раз."""
    async with session_factory() as s:
        ids = set()
        for i in range(20):
            ids.add((await repository.create_job(s, f"p{i}", {})).id)
            await repository.claim_job(s, "dead")
        await s.execute(text("UPDATE jobs SET started_at = now() - interval '1 hour'"))
        await s.commit()

    async def reaper():
        async with session_factory() as s:
            return await _reap(s)

    results = await asyncio.gather(*(reaper() for _ in range(5)))
    requeued = [job_id for r in results for job_id in r.requeued]

    assert len(requeued) == 20
    assert set(requeued) == ids
