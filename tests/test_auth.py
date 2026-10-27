"""Эндпоинты воркера требуют Bearer-токен, клиентские — нет."""

import uuid

import pytest

WORKER_ENDPOINTS = [
    ("/jobs/claim", {"worker_id": "w1"}),
    (f"/jobs/{uuid.uuid4()}/complete", {"worker_id": "w1", "result": "x"}),
    (f"/jobs/{uuid.uuid4()}/fail", {"worker_id": "w1", "error": "x"}),
]

BAD_HEADERS = [
    pytest.param({}, id="no-header"),
    pytest.param({"Authorization": "Bearer wrong-token"}, id="wrong-token"),
    pytest.param({"Authorization": "Bearer "}, id="empty-token"),
    pytest.param({"Authorization": "test-worker-token"}, id="no-scheme"),
    pytest.param({"Authorization": "Basic test-worker-token"}, id="wrong-scheme"),
]


@pytest.mark.parametrize(("path", "payload"), WORKER_ENDPOINTS)
@pytest.mark.parametrize("headers", BAD_HEADERS)
async def test_worker_endpoints_reject_bad_token(client, path, payload, headers):
    resp = await client.post(path, json=payload, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_rejected_claim_does_not_take_job(client, create_job):
    job = await create_job()
    resp = await client.post(
        "/jobs/claim", json={"worker_id": "w1"}, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    assert (await client.get(f"/jobs/{job['id']}")).json()["status"] == "queued"


async def test_valid_token_is_accepted(client, worker_headers):
    resp = await client.post("/jobs/claim", json={"worker_id": "w1"}, headers=worker_headers)
    assert resp.status_code == 204


async def test_client_endpoints_need_no_token(client):
    resp = await client.post("/jobs", json={"prompt": "hi"})
    assert resp.status_code == 201
    resp = await client.get(f"/jobs/{resp.json()['id']}")
    assert resp.status_code == 200
