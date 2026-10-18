"""api.cards carries the frontmatter long tail as a document, not only as flattened facets.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-31 00:00:00.000000

``wiki.pages.frontmatter`` is where a page's long tail lives — the spec-declared per-kind
fields the typed columns do not model. ``api.facets`` unpivots it so a scalar can be
filtered on (``WHERE facet = 'priority'``), but unpivoting is a *filter* shape, not a
*fetch* shape: a value deeper than a scalar has no way back out. The card row carries the
whole document.

One APPENDED column: ``CREATE OR REPLACE VIEW`` keeps the ``wiki_reader`` grants and
permits only appending, so every existing query and column comment stands unchanged.
"""

from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Append ``frontmatter`` to ``api.cards`` and document it."""
    op.execute(
        """
        CREATE OR REPLACE VIEW api.cards AS
        SELECT
          p.id,
          p.kind                              AS type,
          p.title,
          p.summary,
          p.status,
          (p.superseded_by IS NOT NULL)       AS superseded,
          p.entity_type,
          p.paper_type,
          p.maturity,
          p.year,
          p.tasks,
          p.domains,
          p.stages,
          (SELECT count(*) FROM wiki.claims c WHERE c.page_id = p.id)        AS n_claims,
          (SELECT count(*) FROM wiki.edges e WHERE e.src_id = p.id)          AS out_degree,
          (SELECT count(*) FROM wiki.edges e WHERE e.dst_id = p.id)          AS in_degree,
          p.updated,
          p.frontmatter
        FROM wiki.pages p
        """
    )
    op.execute(
        "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document "
        "(the same keys api.facets unpivots, plus values deeper than a scalar). "
        "Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = ''ent.lightrag'';'"
    )


def downgrade() -> None:
    """Recreate ``api.cards`` without the ``frontmatter`` column (a view cannot drop a column in place)."""
    op.execute("DROP VIEW api.cards")
    op.execute(
        """
        CREATE VIEW api.cards AS
        SELECT
          p.id,
          p.kind                              AS type,
          p.title,
          p.summary,
          p.status,
          (p.superseded_by IS NOT NULL)       AS superseded,
          p.entity_type,
          p.paper_type,
          p.maturity,
          p.year,
          p.tasks,
          p.domains,
          p.stages,
          (SELECT count(*) FROM wiki.claims c WHERE c.page_id = p.id)        AS n_claims,
          (SELECT count(*) FROM wiki.edges e WHERE e.src_id = p.id)          AS out_degree,
          (SELECT count(*) FROM wiki.edges e WHERE e.dst_id = p.id)          AS in_degree,
          p.updated
        FROM wiki.pages p
        """
    )
    op.execute("GRANT SELECT ON api.cards TO wiki_reader")
