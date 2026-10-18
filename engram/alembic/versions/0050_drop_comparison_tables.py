"""The comparison table and its view go: no knowledge base declares comparison cells and nothing writes them.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-06 00:00:00.000000

`kb.comparison_cells` and the `api.matrix` view over it are dropped. The indexer never wrote a cell and no schema
declares one, so every live table is empty. A table that still holds rows is refused, naming the count, rather
than dropped with them. Downgrade puts the table, its keys, the guarded view, its comments and the reader's grant
back as they stood at 0049.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_TABLE = """
CREATE TABLE kb.comparison_cells (
    comparison_id text NOT NULL,
    row_entity text NOT NULL,
    dimension text NOT NULL,
    value text NOT NULL,
    claim_refs text[] DEFAULT '{}'::text[] NOT NULL,
    collection text NOT NULL,
    row_entity_collection text NOT NULL,
    CONSTRAINT comparison_cells_pkey PRIMARY KEY (collection, comparison_id, row_entity, dimension),
    CONSTRAINT comparison_cells_collection_comparison_id_fkey FOREIGN KEY (collection, comparison_id)
        REFERENCES kb.pages(collection, id),
    CONSTRAINT comparison_cells_row_entity_collection_row_entity_fkey FOREIGN KEY (row_entity_collection, row_entity)
        REFERENCES kb.pages(collection, id)
)
"""

_VIEW = """
CREATE VIEW api.matrix AS
 SELECT cc.comparison_id,
    comp.title AS comparison_title,
    cc.row_entity,
    re.title AS row_entity_title,
    cc.dimension,
    cc.value,
    cc.claim_refs
   FROM ((kb.comparison_cells cc
     JOIN kb.pages comp ON (((comp.id = cc.comparison_id) AND api.visible_collection(comp.collection))))
     JOIN kb.pages re ON (((re.id = cc.row_entity) AND api.visible_collection(re.collection))))
  WHERE api.visible_collection(cc.collection)
"""

_COMMENTS = (
    "COMMENT ON VIEW api.matrix IS 'Comparison matrices as data: one row per (comparison, row entity, dimension) cell, "
    "with names. Example: SELECT row_entity_title, dimension, value FROM api.matrix WHERE comparison_id = "
    "''cmp.graph-rag-systems'';'",
    "COMMENT ON COLUMN api.matrix.comparison_id IS 'Comparison page id.'",
    "COMMENT ON COLUMN api.matrix.comparison_title IS 'Comparison page title.'",
    "COMMENT ON COLUMN api.matrix.row_entity IS 'Row entity page id.'",
    "COMMENT ON COLUMN api.matrix.row_entity_title IS 'Row entity name.'",
    "COMMENT ON COLUMN api.matrix.dimension IS 'The compared dimension. Example value: ''indexing cost''.'",
    "COMMENT ON COLUMN api.matrix.value IS 'The cell value (text).'",
    "COMMENT ON COLUMN api.matrix.claim_refs IS 'text[] \u2014 claim ids backing this cell.'",
)


def upgrade() -> None:
    """Drop the view and the table, refusing while the table still holds a row."""
    held = op.get_bind().execute(sa.text("SELECT count(*) FROM kb.comparison_cells")).scalar_one()
    if held:
        msg = (
            f"kb.comparison_cells holds {held} row(s). Nothing in engram reads or writes them any more, and this "
            "revision drops the table. Export them first, then delete them, then upgrade."
        )
        raise RuntimeError(msg)
    op.execute("DROP VIEW api.matrix")
    op.execute("DROP TABLE kb.comparison_cells")


def downgrade() -> None:
    """Put the table, the view, its comments and the reader's grant back as they stood at 0049."""
    op.execute(_TABLE)
    op.execute(_VIEW)
    for comment in _COMMENTS:
        op.execute(comment)
    op.execute(f"GRANT SELECT ON TABLE api.matrix TO {_READER_ROLE}")
