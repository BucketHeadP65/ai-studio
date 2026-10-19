"""`public.schedules` keeps a schedule's gate behaviour in `gate_mode` alone, and every finished refresh stores its pass document.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-19 00:00:00.000000

`auto_approve_gates` said only what `gate_mode = 'auto'` says. A row whose switch is on runs auto, so its `gate_mode`
becomes `'auto'` before the column is dropped, and every other row keeps its mode. Downgrade adds the column back and
derives it from `gate_mode`, true exactly when the mode is `'auto'`, with 0030's comment on `gate_mode` set again.

A finished `kb.refresh` job stores its result as the JSON document a clean pass writes, a one-line `detail` and its
`counts`. A result stored as plain text becomes that document, the text as its detail and every count zero. Downgrade
leaves the documents as they are.
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

_REFRESH_RESULTS_AS_DOCUMENTS = """
UPDATE coordination.jobs
SET result = jsonb_build_object(
    'detail', result,
    'counts', jsonb_build_object('sources_created', 0, 'requested_by_entry', '{}'::jsonb, 'created_by_connector', '{}'::jsonb)
)::text
WHERE kind = 'kb.refresh' AND state = 'done' AND result IS NOT NULL AND result <> '' AND NOT pg_input_is_valid(result, 'jsonb')
"""


def upgrade() -> None:
    """Fold `auto_approve_gates` into `gate_mode` and drop it, then store every text refresh result as its pass document."""
    op.execute("UPDATE public.schedules SET gate_mode = 'auto' WHERE auto_approve_gates AND gate_mode <> 'auto'")
    op.execute("ALTER TABLE public.schedules DROP COLUMN auto_approve_gates")
    op.execute(_GATE_MODE_COMMENT)
    op.execute(_REFRESH_RESULTS_AS_DOCUMENTS)


def downgrade() -> None:
    """Add `auto_approve_gates` back, derived from `gate_mode`, and restore 0030's comment."""
    op.execute("ALTER TABLE public.schedules ADD COLUMN auto_approve_gates boolean NOT NULL DEFAULT false")
    op.execute("UPDATE public.schedules SET auto_approve_gates = (gate_mode = 'auto')")
    op.execute(_PREVIOUS_GATE_MODE_COMMENT)
