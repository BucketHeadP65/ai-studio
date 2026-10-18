"""`kb.pages`'s `superseded_by` key is checked when the transaction commits, not at the end of each statement.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-18 00:00:00.000000

The indexer writes pages in several insert statements once a knowledge base has more pages than one statement can bind.
A page whose `superseded_by` names a page first written by a later statement of the same transaction failed the key at
the end of its own statement. `pages_collection_superseded_by_fkey` becomes `DEFERRABLE INITIALLY DEFERRED`, so the key
is checked at commit, when every page of the index is written. A delete that leaves a page naming a removed page still
fails, at commit. Downgrade makes the key `NOT DEFERRABLE` again, checked at the end of each statement.
"""

from __future__ import annotations

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_DEFER = "ALTER TABLE kb.pages ALTER CONSTRAINT pages_collection_superseded_by_fkey DEFERRABLE INITIALLY DEFERRED"
_RESTORE = "ALTER TABLE kb.pages ALTER CONSTRAINT pages_collection_superseded_by_fkey NOT DEFERRABLE"


def upgrade() -> None:
    """Check the `superseded_by` key at commit."""
    op.execute(_DEFER)


def downgrade() -> None:
    """Check the `superseded_by` key at the end of each statement again."""
    op.execute(_RESTORE)
