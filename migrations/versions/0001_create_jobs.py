"""create jobs table

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # TEXT + CHECK вместо ENUM: новый статус добавляется обычной миграцией
        # (DROP/ADD CONSTRAINT), без особенностей ALTER TYPE ... ADD VALUE.
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_jobs_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts"),
        # Инвариант: у running-задания всегда есть владелец и время старта.
        sa.CheckConstraint(
            "status <> 'running' OR (worker_id IS NOT NULL AND started_at IS NOT NULL)",
            name="ck_jobs_running_has_worker",
        ),
    )

    # Для claim: «самое старое queued». В индекс попадают только queued-строки,
    # поэтому он остаётся маленьким, даже когда завершённых заданий миллионы.
    op.create_index(
        "ix_jobs_queued_created_at",
        "jobs",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    # Для reaper: «running, запущено раньше X».
    op.create_index(
        "ix_jobs_running_started_at",
        "jobs",
        ["started_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_running_started_at", table_name="jobs")
    op.drop_index("ix_jobs_queued_created_at", table_name="jobs")
    op.drop_table("jobs")
