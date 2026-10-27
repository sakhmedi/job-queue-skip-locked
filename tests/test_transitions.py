"""State machine: queued → running → succeeded | failed, и что запрещено (409/404/422)."""

import uuid

import pytest


async def test_create_then_claim(client, create_job, claim):
    job = await create_job("song")
    assert job["status"] == "queued"

    resp = await claim("worker-a")
    assert resp.status_code == 200
    claimed = resp.json()
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["worker_id"] == "worker-a"
    assert claimed["attempts"] == 1
    assert claimed["started_at"] is not None

    # Статус виден клиенту
    assert (await client.get(f"/jobs/{job['id']}")).json()["status"] == "running"


async def test_claimed_job_is_not_given_again(create_job, claim):
    await create_job()
    assert (await claim("a")).status_code == 200
    assert (await claim("b")).status_code == 204


async def test_complete(client, create_job, claim, worker_headers):
    job = await create_job()
    await claim("w1")

    resp = await client.post(
        f"/jobs/{job['id']}/complete",
        json={"worker_id": "w1", "result": "audio.mp3"},
        headers=worker_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result"] == "audio.mp3"
    assert body["error"] is None
    assert body["finished_at"] is not None


async def test_fail(client, create_job, claim, worker_headers):
    job = await create_job()
    await claim("w1")

    resp = await client.post(
        f"/jobs/{job['id']}/fail",
        json={"worker_id": "w1", "error": "CUDA out of memory"},
        headers=worker_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "CUDA out of memory"
    assert body["result"] is None
    assert body["finished_at"] is not None


@pytest.mark.parametrize("first", ["complete", "fail"])
@pytest.mark.parametrize("second", ["complete", "fail"])
async def test_finishing_twice_is_409(client, create_job, claim, worker_headers, first, second):
    job = await create_job()
    await claim("w1")
    payloads = {
        "complete": {"worker_id": "w1", "result": "ok"},
        "fail": {"worker_id": "w1", "error": "boom"},
    }

    r1 = await client.post(
        f"/jobs/{job['id']}/{first}", json=payloads[first], headers=worker_headers
    )
    assert r1.status_code == 200
    r2 = await client.post(
        f"/jobs/{job['id']}/{second}", json=payloads[second], headers=worker_headers
    )
    assert r2.status_code == 409
    assert "only 'running' jobs" in r2.json()["detail"]

    # Первый результат не перезаписан
    final = (await client.get(f"/jobs/{job['id']}")).json()
    assert final["status"] == ("succeeded" if first == "complete" else "failed")


async def test_complete_by_other_worker_is_409(client, create_job, claim, worker_headers):
    job = await create_job()
    await claim("owner")

    resp = await client.post(
        f"/jobs/{job['id']}/complete",
        json={"worker_id": "intruder", "result": "x"},
        headers=worker_headers,
    )
    assert resp.status_code == 409
    assert "held by worker 'owner'" in resp.json()["detail"]
    assert (await client.get(f"/jobs/{job['id']}")).json()["status"] == "running"


@pytest.mark.parametrize("action", ["complete", "fail"])
async def test_finish_queued_job_is_409(client, create_job, worker_headers, action):
    job = await create_job()
    resp = await client.post(
        f"/jobs/{job['id']}/{action}",
        json={"worker_id": "w1", "result": "x", "error": "x"},
        headers=worker_headers,
    )
    assert resp.status_code == 409
    assert "'queued'" in resp.json()["detail"]


@pytest.mark.parametrize("action", ["complete", "fail"])
async def test_finish_unknown_job_is_404(client, worker_headers, action):
    resp = await client.post(
        f"/jobs/{uuid.uuid4()}/{action}",
        json={"worker_id": "w1", "result": "x", "error": "x"},
        headers=worker_headers,
    )
    assert resp.status_code == 404


async def test_finish_invalid_uuid_is_422(client, worker_headers):
    resp = await client.post(
        "/jobs/123/complete", json={"worker_id": "w1", "result": "x"}, headers=worker_headers
    )
    assert resp.status_code == 422


async def test_fifo_order(create_job, claim):
    ids = [(await create_job(f"job {i}"))["id"] for i in range(5)]
    claimed = [(await claim("w1")).json()["id"] for _ in range(5)]
    assert claimed == ids
