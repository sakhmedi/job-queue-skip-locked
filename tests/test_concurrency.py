"""Гонки — главное в этом проекте.

Каждый «воркер» здесь — отдельная сессия = отдельное соединение = отдельная транзакция
(см. фикстуру session_factory с NullPool). asyncio.gather запускает их одновременно,
как горутины + sync.WaitGroup в Go.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import repository


async def _claim_in_own_session(session_factory, worker_id):
    async with session_factory() as s:
        return await repository.claim_job(s, worker_id)


async def _create(session_factory, prompt="p"):
    async with session_factory() as s:
        return await repository.create_job(s, prompt, {})


@pytest.mark.parametrize("n_workers", [2, 10])
async def test_parallel_claims_on_single_job(session_factory, n_workers):
    job = await _create(session_factory)

    results = await asyncio.gather(
        *(_claim_in_own_session(session_factory, f"w{i}") for i in range(n_workers))
    )

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"job was given to {len(winners)} workers"
    assert winners[0].id == job.id
    assert winners[0].attempts == 1


async def test_100_jobs_10_workers_each_job_exactly_once(session_factory):
    created = {(await _create(session_factory, f"job {i}")).id for i in range(100)}

    async def worker(worker_id):
        got = []
        while True:
            job = await _claim_in_own_session(session_factory, worker_id)
            if job is None:
                return got
            got.append(job.id)

    per_worker = await asyncio.gather(*(worker(f"w{i}") for i in range(10)))
    all_claimed = [job_id for ids in per_worker for job_id in ids]

    assert len(all_claimed) == 100  # ни одного повтора...
    assert set(all_claimed) == created  # ...и ни одного потерянного
    # Работа действительно распределилась между несколькими воркерами
    assert sum(1 for ids in per_worker if ids) > 1

    async with session_factory() as s:
        rows = (
            await s.execute(text("SELECT status, attempts, count(*) FROM jobs GROUP BY 1, 2"))
        ).all()
    assert rows == [("running", 1, 100)]


async def test_claim_skips_row_locked_by_another_transaction(session_factory):
    older = await _create(session_factory, "older")
    newer = await _create(session_factory, "newer")

    async with session_factory() as locker:
        # Открываем транзакцию и держим блокировку на самом старом задании.
        await locker.execute(
            text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": older.id}
        )

        # Другой воркер не ждёт освобождения строки, а сразу берёт следующую.
        # wait_for — страховка: без SKIP LOCKED тест повис бы, а не прошёл.
        job = await asyncio.wait_for(_claim_in_own_session(session_factory, "w2"), timeout=5)
        assert job is not None
        assert job.id == newer.id

        # Оба queued-задания заблокированы/заняты → очередь «пуста» для этого воркера.
        assert await _claim_in_own_session(session_factory, "w3") is None

        await locker.rollback()  # отпускаем блокировку

    job = await _claim_in_own_session(session_factory, "w3")
    assert job is not None
    assert job.id == older.id


async def test_without_skip_locked_claim_would_wait(session_factory):
    """Для сравнения: обычный FOR UPDATE встаёт в очередь за чужой блокировкой."""
    await _create(session_factory)

    async with session_factory() as locker, session_factory() as other:
        await locker.execute(text("SELECT id FROM jobs FOR UPDATE"))

        await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
        with pytest.raises(DBAPIError, match="lock timeout"):
            await other.execute(
                text("SELECT id FROM jobs WHERE status = 'queued' LIMIT 1 FOR UPDATE")
            )


async def test_naive_select_then_update_gives_job_twice(session_factory):
    """Почему «сначала SELECT, потом UPDATE» сломан: оба воркера видят одно задание."""
    job = await _create(session_factory)
    pick = text("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1")
    take = text(
        "UPDATE jobs SET status = 'running', worker_id = :w, started_at = now() WHERE id = :id"
    )

    async with session_factory() as a, session_factory() as b:
        id_a = await a.scalar(pick)
        id_b = await b.scalar(pick)  # B успел прочитать до того, как A записал
        assert id_a == id_b == job.id

        res_a = await a.execute(take, {"w": "a", "id": id_a})
        await a.commit()
        res_b = await b.execute(take, {"w": "b", "id": id_b})
        await b.commit()

    # Оба UPDATE «успешны» → задание выполнят два воркера, а B ещё и затёр владельца.
    assert res_a.rowcount == 1
    assert res_b.rowcount == 1
