"""Pydantic-схемы: что API принимает и что отдаёт.

Невалидное тело запроса FastAPI сам превращает в 422 с описанием ошибки.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import JobStatus

WorkerId = Annotated[str, Field(min_length=1, max_length=200)]


class JobCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    params: dict[str, Any] = Field(default_factory=dict)


class ClaimRequest(BaseModel):
    worker_id: WorkerId


class CompleteRequest(BaseModel):
    worker_id: WorkerId
    result: str


class FailRequest(BaseModel):
    worker_id: WorkerId
    error: str


class JobOut(BaseModel):
    # from_attributes: можно отдать ORM-объект Job, Pydantic прочитает его атрибуты.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prompt: str
    params: dict[str, Any]
    status: JobStatus
    result: str | None
    error: str | None
    worker_id: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
