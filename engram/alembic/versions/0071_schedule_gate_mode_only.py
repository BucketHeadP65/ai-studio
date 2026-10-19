"""`public.schedules` keeps a schedule's gate behaviour in `gate_mode` alone.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-19 00:00:00.000000

`auto_approve_gates` said only what `gate_mode = 'auto'` says. A row whose switch is on runs auto, so its `gate_mode`
becomes `'auto'` before the column is dropped, and every other row keeps its mode. Downgrade adds the column back and
derives it from `gate_mode`, true exactly when the mode is `'auto'`, with 0030's comment on `gate_mode` set again.
"""

from __future__ import annotations

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_GATE_MODE_COMMENT = (
    "COMMENT ON COLUMN public.schedules.gate_mode IS "
    "'wait parks a person gate for a person, orchestrate hands it to the run''s own orchestrator, "
    "auto passes it with nobody asked.'"
)

# 0030's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_GATE_MODE_COMMENT = (
    "COMMENT ON COLUMN public.schedules.gate_mode IS "
    "'wait parks a human gate, orchestrate hands it to the run''s own orchestrator, "
    "auto passes it with nobody asked -- the same thing auto_approve_gates true means.'"
)


def upgrade() -> None:
    """Fold `auto_approve_gates` into `gate_mode`, then drop it."""
    op.execute("UPDATE public.schedules SET gate_mode = 'auto' WHERE auto_approve_gates AND gate_mode <> 'auto'")
    op.execute("ALTER TABLE public.schedules DROP COLUMN auto_approve_gates")
    op.execute(_GATE_MODE_COMMENT)


def downgrade() -> None:
    """Add `auto_approve_gates` back, derived from `gate_mode`, and restore 0030's comment."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN auto_approve_gates boolean NOT NULL DEFAULT false")
    op.execute("UPDATE public.schedules SET auto_approve_gates = (gate_mode = 'auto')")
    op.execute(_PREVIOUS_GATE_MODE_COMMENT)
