"""`api.collection_about` matches the compiler's collection-scoped orientation id.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-19 00:00:00.000000

`memory.pages.id` is one primary key shared by every collection compiled into the same database
(`collection` is a plain column, not part of the key). The synthetic orientation page's id
(`engram.compile.handlers.ORIENTATION_PAGES`) used to be the literal `meta.about` /
`meta.authoring` for every collection, so a second collection's `ON CONFLICT (id) DO UPDATE`
silently overwrote a first collection's orientation row -- `api.collection_about('repo-docs')`
returning zero rows after a later `field-notes` compile, even though `repo-docs` had genuinely
compiled its own about text, was the live symptom.

The compiler now writes `orientation_page_id(base_id, collection)`: the bare literal for the
`research` collection (the one `api.about` / `api.authoring_rules` / `api.schema_guide` still read
unscoped, migrations 0002/0008), `f"{base_id}@{collection}"` for every other collection. This
revision replaces `api.collection_about` so its lookup matches the same scheme -- the function's
OUT columns and external contract (call with `p_collection`, get that collection's about text) are
unchanged, so `CREATE OR REPLACE FUNCTION` applies directly.
"""

from __future__ import annotations

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SIGNATURE = "api.collection_about(text)"

_COMMENT = (
    "COMMENT ON FUNCTION api.collection_about(text) IS "
    "'One collection''s about text (its compiled meta.about page), served as the collection map''s "
    "own content. Zero rows when the collection compiled none. Example: "
    "SELECT body FROM api.collection_about(''research'');'"
)


def upgrade() -> None:
    """Replace `api.collection_about` to match the compiler's collection-scoped orientation id."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION api.collection_about(p_collection text)
          RETURNS TABLE (body text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.card_text AS body
            FROM memory.pages p
            WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p_collection END)
              AND p.collection = p_collection
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_COMMENT)


def downgrade() -> None:
    """Restore 0018's unscoped-literal-id lookup."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION api.collection_about(p_collection text)
          RETURNS TABLE (body text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.card_text AS body
            FROM memory.pages p
            WHERE p.id = 'meta.about' AND p.collection = p_collection
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_COMMENT)
