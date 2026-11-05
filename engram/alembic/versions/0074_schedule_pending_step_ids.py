"""`public.schedules` gains `pending_step_ids`, the changed steps an on-step-change schedule collected and no fire has passed on yet.

Revision ID: 0074
Revises: 0073
Create Date: 2026-11-05 00:00:00.000000

One text array column, empty by default, so every existing row reads unchanged. An on-step-change arm adds the reusable
step version its event names to the array, each id once. A fire that launches a run passes the ids it holds and its
stamp removes exactly those ids, so a step a later event added while the fire was underway stays for the next fire.
Downgrade drops the column.
"""

from __future__ import annotations

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add `pending_step_ids`, empty on every existing row."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN pending_step_ids text[] NOT NULL DEFAULT '{}'")
    op.execute(
        "COMMENT ON COLUMN public.schedules.pending_step_ids IS "
        "'The changed steps the on-step-change arms collected that no launched fire has passed on yet, each id once.'"
    )


def downgrade() -> None:
    """Drop `pending_step_ids`."""
    op.execute("ALTER TABLE public.schedules DROP COLUMN pending_step_ids")
