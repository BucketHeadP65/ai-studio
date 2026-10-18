"""`kb.cut_plans` stores the cut plans a chunk planner chose, so indexing the same text again calls no model.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-18 00:00:00.000000

A chunk planner reads a run of mirror text with no heading as numbered paragraphs and answers the numbers of the
paragraphs where a new chunk starts. The indexer stores each accepted answer keyed by collection, the sha256 of the
text and the planner's model name and prompt version, and reuses it whenever the same text meets the same planner.
`starts` holds the paragraph numbers, paragraph 1 left out. No reader is granted the table. Downgrade drops it, and
the next index asks the planner again.
"""

from __future__ import annotations

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_TABLE = """
CREATE TABLE kb.cut_plans (
    collection text NOT NULL,
    text_hash text NOT NULL,
    planner text NOT NULL,
    starts integer[] NOT NULL,
    CONSTRAINT cut_plans_pkey PRIMARY KEY (collection, text_hash, planner)
)
"""

_COMMENT = (
    "COMMENT ON TABLE kb.cut_plans IS "
    "'The cut plans a chunk planner chose: the paragraph numbers where a new chunk starts in one run of mirror text, "
    "paragraph 1 left out, by collection, the sha256 of the text and the planner''s model name and prompt version. "
    "The indexer reuses a stored plan instead of asking the planner again. No reader is granted it.'"
)


def upgrade() -> None:
    """Create the cut plan table."""
    op.execute(_TABLE)
    op.execute(_COMMENT)


def downgrade() -> None:
    """Drop the cut plan table."""
    op.execute("DROP TABLE kb.cut_plans")
