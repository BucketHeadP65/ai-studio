"""`public.schedules` gains shared capacity pools, daily blackout windows, and catch-up.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-24 00:00:00.000000

Four columns, all nullable or defaulted so every existing row reads unchanged: `capacity_key`
and `capacity_limit` are a pair -- both null means unlimited, exactly today's behavior -- naming
a concurrency budget a scheduler ticker shares across every schedule that names the same key.
`blackout` names one or more daily "HH:MM-HH:MM" quiet-hours windows a fire is suppressed inside.
`catch_up` decides what a held fire does once it clears: false (the default) skips ahead to the
cadence's next legitimate occurrence; true fires once, immediately, the first tick it can.
"""

from __future__ import annotations

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the four columns to `public.schedules`, all nullable or defaulted."""
    op.execute(
        """
        ALTER TABLE public.schedules
          ADD COLUMN capacity_key   text,
          ADD COLUMN capacity_limit integer,
          ADD COLUMN blackout       text,
          ADD COLUMN catch_up       boolean NOT NULL DEFAULT false
        """
    )
    op.execute(
        "COMMENT ON COLUMN public.schedules.capacity_key IS "
        "'Groups schedules sharing one concurrency budget; null means this schedule joins no pool.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.schedules.capacity_limit IS "
        "'How many schedules sharing capacity_key may have a live run at once; null means unlimited.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.schedules.blackout IS "
        "'One or more daily HH:MM-HH:MM quiet-hours windows, comma-separated; null means none.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.schedules.catch_up IS "
        "'Whether a held fire (overlap or blackout) fires once immediately once clear, rather than skipping ahead.'"
    )


def downgrade() -> None:
    """Drop the four columns."""
    op.execute(
        """
        ALTER TABLE public.schedules
          DROP COLUMN IF EXISTS capacity_key,
          DROP COLUMN IF EXISTS capacity_limit,
          DROP COLUMN IF EXISTS blackout,
          DROP COLUMN IF EXISTS catch_up
        """
    )
