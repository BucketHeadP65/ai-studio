"""A spent job is `abandoned`, not `parked`.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-03 00:00:00.000000

`coordination.jobs.state` answers `abandoned` when a job has used up its attempts. The check
constraint `ck_jobs_state` is dropped, every row carrying the old value is rewritten, and the
constraint is recreated over the new vocabulary.
"""

from __future__ import annotations

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Rewrite the spent-job state and the constraint that admits it."""
    op.execute("ALTER TABLE coordination.jobs DROP CONSTRAINT ck_jobs_state")
    op.execute("UPDATE coordination.jobs SET state = 'abandoned' WHERE state = 'parked'")
    op.execute(
        "ALTER TABLE coordination.jobs ADD CONSTRAINT ck_jobs_state "
        "CHECK (state IN ('queued', 'running', 'failed', 'done', 'abandoned'))"
    )


def downgrade() -> None:
    """Restore the vocabulary 0034 created."""
    op.execute("ALTER TABLE coordination.jobs DROP CONSTRAINT ck_jobs_state")
    op.execute("UPDATE coordination.jobs SET state = 'parked' WHERE state = 'abandoned'")
    op.execute(
        "ALTER TABLE coordination.jobs ADD CONSTRAINT ck_jobs_state "
        "CHECK (state IN ('queued', 'running', 'failed', 'done', 'parked'))"
    )
