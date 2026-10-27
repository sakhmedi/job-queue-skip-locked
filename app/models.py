"""ORM-модель таблицы `jobs`.

Схема в БД создаётся миграцией (migrations/versions/0001_create_jobs.py),
а эта модель описывает ту же таблицу для Python-кода. Их нужно держать в синхронизации.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    prompt: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        Text, default=JobStatus.QUEUED, server_default=JobStatus.QUEUED
    )
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_jobs_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        CheckConstraint(
            "status <> 'running' OR (worker_id IS NOT NULL AND started_at IS NOT NULL)",
            name="ck_jobs_running_has_worker",
        ),
        Index(
            "ix_jobs_queued_created_at",
            "created_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_jobs_running_started_at",
            "started_at",
            postgresql_where=text("status = 'running'"),
        ),
    )
