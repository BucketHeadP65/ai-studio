"""`memory.compile_fingerprint` gains `corpus_root`, the corpus root that compiled the collection.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-28 00:00:00.000000

One text column, empty until a compile stamps it. A compile whose corpus root differs from the
stored one refuses to write unless the caller named the collection explicitly, so one corpus can
never silently prune another corpus's collection. Every existing row starts empty (unclaimed) and
is claimed by its next compile.
"""

from __future__ import annotations

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add `corpus_root`, empty for every existing row so each collection reads as unclaimed."""
    op.execute("ALTER TABLE memory.compile_fingerprint ADD COLUMN corpus_root text NOT NULL DEFAULT ''")
    op.execute(
        "COMMENT ON COLUMN memory.compile_fingerprint.corpus_root IS "
        "'The corpus root that compiled this collection; empty until a compile stamps it.'"
    )


def downgrade() -> None:
    """Drop `corpus_root`."""
    op.execute("ALTER TABLE memory.compile_fingerprint DROP COLUMN IF EXISTS corpus_root")
