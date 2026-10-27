"""HTTP-эндпоинты очереди.

Роутер тонкий: валидация (Pydantic), вызов repository и выбор HTTP-кода.
JobNotFoundError / JobConflictError превращаются в 404 / 409 обработчиками в app/main.py.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import require_worker_token
from app.db import get_session
from app.models import Job
from app.schemas import ClaimRequest, CompleteRequest, FailRequest, JobCreate, JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
WORKER_AUTH = [Depends(require_worker_token)]


# --- клиентские эндпоинты (без токена) ---


@router.post("", status_code=status.HTTP_201_CREATED, response_model=JobOut)
async def create_job(body: JobCreate, session: SessionDep) -> Job:
    return await repository.create_job(session, body.prompt, body.params)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    # job_id: uuid.UUID — FastAPI сам вернёт 422 на строку, не похожую на UUID.
    job = await repository.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


# --- эндпоинты воркера (нужен Bearer-токен) ---


@router.post(
    "/claim",
    response_model=JobOut,
    dependencies=WORKER_AUTH,
    responses={204: {"description": "Очередь пуста"}},
)
async def claim_job(body: ClaimRequest, session: SessionDep) -> Job | Response:
    job = await repository.claim_job(session, body.worker_id)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return job


@router.post("/{job_id}/complete", response_model=JobOut, dependencies=WORKER_AUTH)
async def complete_job(job_id: uuid.UUID, body: CompleteRequest, session: SessionDep) -> Job:
    return await repository.complete_job(session, job_id, body.worker_id, body.result)


@router.post("/{job_id}/fail", response_model=JobOut, dependencies=WORKER_AUTH)
async def fail_job(job_id: uuid.UUID, body: FailRequest, session: SessionDep) -> Job:
    return await repository.fail_job(session, job_id, body.worker_id, body.error)
