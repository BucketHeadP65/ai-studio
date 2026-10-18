"""`public.schedules` gains the three-way `gate_mode`, alongside the legacy `auto_approve_gates`.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-24 00:00:00.000000

One column, defaulted so every existing row reads as it always has: `"wait"` parks a human gate
for a person, `"orchestrate"` hands one to the run's own orchestrator instead of parking it,
`"auto"` passes every gate with nobody asked -- the same thing `auto_approve_gates=true` already
meant. `auto_approve_gates` itself is untouched; the two columns are kept in agreement by the
application, never by a database constraint.
"""

from __future__ import annotations

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add `gate_mode`, defaulted to `'wait'`, so every existing row reads unchanged."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN gate_mode text NOT NULL DEFAULT 'wait'")
    op.execute(
        "COMMENT ON COLUMN public.schedules.gate_mode IS "
        "'wait parks a human gate, orchestrate hands it to the run''s own orchestrator, "
        "auto passes it with nobody asked -- the same thing auto_approve_gates true means.'"
    )


def downgrade() -> None:
    """Drop `gate_mode`."""
    op.execute("ALTER TABLE public.schedules DROP COLUMN IF EXISTS gate_mode")
