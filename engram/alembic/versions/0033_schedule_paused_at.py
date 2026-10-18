"""`public.schedules` gains `paused_at`, the moment a person's own pause last took hold.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-25 00:00:00.000000

One nullable timestamptz column: null while a schedule is enabled, or if it has never been
disabled. The PATCH door (`PostgresSchedules.update`) stamps it with the current instant the
moment `enabled` flips true to false, and clears it back to null the moment `enabled` flips false
to true; any other patch leaves it alone. A schedule minted already disabled is stamped the same
way at creation.
"""

from __future__ import annotations

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add `paused_at`, nullable, so every existing row reads unchanged (null, never paused)."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN paused_at timestamptz")
    op.execute(
        "COMMENT ON COLUMN public.schedules.paused_at IS "
        "'The moment enabled last flipped true to false; null while enabled, or if it never has.'"
    )


def downgrade() -> None:
    """Drop `paused_at`."""
    op.execute("ALTER TABLE public.schedules DROP COLUMN IF EXISTS paused_at")
