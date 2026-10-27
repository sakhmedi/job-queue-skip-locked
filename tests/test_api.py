"""Базовое поведение HTTP API: валидация, коды ответов, формат."""

import uuid

import pytest


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


async def test_create_job_returns_201_and_queued(client):
    resp = await client.post("/jobs", json={"prompt": "lo-fi beat", "params": {"bpm": 90}})
    assert resp.status_code == 201
    body = resp.json()
    uuid.UUID(body["id"])  # валидный UUID
    assert body["status"] == "queued"
    assert body["prompt"] == "lo-fi beat"
    assert body["params"] == {"bpm": 90}
    assert body["attempts"] == 0
    assert body["worker_id"] is None
    assert body["started_at"] is None
    assert body["finished_at"] is None


async def test_create_job_default_params(client):
    resp = await client.post("/jobs", json={"prompt": "x"})
    assert resp.status_code == 201
    assert resp.json()["params"] == {}


async def test_prompt_max_length_is_allowed(client):
    resp = await client.post("/jobs", json={"prompt": "a" * 4000})
    assert resp.status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {},  # нет prompt
        {"prompt": ""},  # пустой
        {"prompt": "a" * 4001},  # слишком длинный
        {"prompt": "x", "params": [1, 2]},  # params не объект
        {"prompt": 123},  # не строка
    ],
)
async def test_create_job_validation(client, payload):
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 422


async def test_get_job(client, create_job):
    job = await create_job("hello")
    resp = await client.get(f"/jobs/{job['id']}")
    assert resp.status_code == 200
    assert resp.json() == job


async def test_get_unknown_job_404(client):
    resp = await client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_invalid_uuid_422(client):
    resp = await client.get("/jobs/not-a-uuid")
    assert resp.status_code == 422


async def test_claim_empty_queue_204(claim):
    resp = await claim("w1")
    assert resp.status_code == 204
    assert resp.content == b""


async def test_claim_requires_worker_id(client, worker_headers):
    resp = await client.post("/jobs/claim", json={}, headers=worker_headers)
    assert resp.status_code == 422
