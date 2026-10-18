"""`api.cards.frontmatter` says where the declared link fields went: `api.edges`, not the jsonb.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-01 00:00:00.000000

The long-tail projection drops every link-shaped frontmatter value (a flag's `about`, a digest's
`same_as`, any `edge_fields` entry of the kind) because each is already a row in `memory.edges`.
A reader who looks a declared link up in `frontmatter` gets NULL and no hint. The column comment
now names the rule and the view that holds them, so `describe_schema` teaches it at the column a
reader actually queries.
"""

from __future__ import annotations

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Name the edge rule on `api.cards.frontmatter`."""
    op.execute(
        "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys "
        "api.facets unpivots, plus values deeper than a scalar). Declared link and link-list fields (the kind''s edge_fields in "
        "api.page_types, e.g. a flag''s about or resolved_by) are NOT here: each is a row in api.edges with rel = the field name, "
        "filter on src_id. Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = ''ent.lightrag'';'"
    )


def downgrade() -> None:
    """Restore the comment that said nothing about link fields."""
    op.execute(
        "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys "
        "api.facets unpivots, plus values deeper than a scalar). Example: SELECT frontmatter->''priority'' FROM api.cards WHERE "
        "id = ''ent.lightrag'';'"
    )
