"""`memory.compile_manifest.zotero_key` becomes `source_key`, and its index follows the name.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-02 00:00:00.000000

The column is the mirror-doc key every compiled `Sources/*.md` file's manifest row carries (the
value `MirrorDoc.source_key` derives from the mirror's own `source_key` frontmatter field), and the
join key the mirror-citer closure and the targeted source delete are keyed by. It is engram's own
compile-state name, not a corpus-declared field, and no `api` view or function reads it, so the
rename is the column plus the covering index `(collection, source_key)`.
"""

from __future__ import annotations

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Rename the column to `source_key` and its index to match."""
    op.execute("ALTER TABLE memory.compile_manifest RENAME COLUMN zotero_key TO source_key")
    op.execute("ALTER INDEX memory.ix_compile_manifest_ztk RENAME TO ix_compile_manifest_source_key")


def downgrade() -> None:
    """Restore the column and index names 0006 created."""
    op.execute("ALTER INDEX memory.ix_compile_manifest_source_key RENAME TO ix_compile_manifest_ztk")
    op.execute("ALTER TABLE memory.compile_manifest RENAME COLUMN source_key TO zotero_key")
