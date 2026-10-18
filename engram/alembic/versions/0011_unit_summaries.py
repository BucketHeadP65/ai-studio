"""Add memory.unit_summaries for compiled, governed disclosure summaries.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09 00:00:00.000000

Compiled summaries are rows, not frontmatter: they carry provenance (the
compiling `model` and the `source_hash` the summary was compiled from) and the
`pending` disclosure-governance quarantine — a summary starts `pending` and
only governance (`engram summaries approve`) promotes it, so nothing written
here is ever disclosed until reviewed. `unit_id` is the primary key (one
summary per unit, no history); `unit_summaries_collection_status` indexes the
`(collection, status)` pair the governance queue scans ("find the next pending
summary in this collection").
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE memory.unit_summaries (
            unit_id     text NOT NULL,
            unit_kind   text NOT NULL,
            collection  text NOT NULL,
            summary     text NOT NULL,
            status      text NOT NULL DEFAULT 'pending',
            model       text NOT NULL,
            source_hash text NOT NULL,
            compiled_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (unit_id)
        )
    """)
    op.execute("CREATE INDEX unit_summaries_collection_status ON memory.unit_summaries (collection, status)")


def downgrade() -> None:
    op.execute("DROP TABLE memory.unit_summaries")
