"""api.collection_about: one collection's about text, for its map.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-17 00:00:00.000000

``api.about`` answers with the database's single ``meta.about`` page, so a database holding several
collections cannot tell a caller which corpus the text describes. The leveled root map needs the
about scoped to the collection being mapped: ``api.collection_about(p_collection)`` returns that
collection's own ``meta.about`` card text, and zero rows when the collection compiled none. Grants
and the ``COMMENT ON`` follow 0012's pattern, against the post-0014 reader role.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_READER_ROLE = "corpus_reader"

_SIGNATURE = "api.collection_about(text)"

_BODY = """
CREATE FUNCTION api.collection_about(p_collection text)
  RETURNS TABLE (body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = 'meta.about' AND p.collection = p_collection
$$
"""

_COMMENT = (
    "COMMENT ON FUNCTION api.collection_about(text) IS "
    "'One collection''s about text (its compiled meta.about page), served as the collection map''s "
    "own content. Zero rows when the collection compiled none. Example: "
    "SELECT body FROM api.collection_about(''research'');'"
)


def upgrade() -> None:
    """Create api.collection_about and grant it to the reader role."""
    op.execute(_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_COMMENT)


def downgrade() -> None:
    """Drop api.collection_about."""
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
