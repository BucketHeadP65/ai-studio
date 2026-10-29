"""`public.schedules` gains `pending_page_ids`, the changed pages an on-index schedule collected and no fire has passed on yet.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-29 00:00:00.000000

One text array column, empty by default, so every existing row reads unchanged. An on-index arm adds the changed pages
of its index to the array, each id once. A fire that launches a run passes the ids it holds and its stamp removes
exactly those ids, so a page an index added while the fire was underway stays for the next fire. Downgrade drops the
column.
"""

from __future__ import annotations

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add `pending_page_ids`, empty on every existing row."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN pending_page_ids text[] NOT NULL DEFAULT '{}'")
    op.execute(
        "COMMENT ON COLUMN public.schedules.pending_page_ids IS "
        "'The changed pages the on-index arms collected that no launched fire has passed on yet, each id once.'"
    )


def downgrade() -> None:
    """Drop `pending_page_ids`."""
    op.execute("ALTER TABLE public.schedules DROP COLUMN pending_page_ids")
