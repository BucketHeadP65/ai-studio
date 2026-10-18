"""api.envelope_collection filters corpusspec-excluded kinds.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-09 00:00:00.000000

The corpus spec declares per page type whether its units are disclosed (`PageTypeSpec.disclosed`);
`EnvelopeHandler` enforces the declaration by binding the excluded kinds into the collection root
map. The one-argument ``api.envelope_collection(text)`` (0012) is replaced by a two-argument form
whose body adds ``AND p.kind <> ALL(p_excluded_kinds)`` — everything else (the summary
``COALESCE``, the join-predicate status filter, the ordering) is unchanged. Grants and the
``COMMENT ON`` follow 0012's pattern; downgrade restores the one-argument function verbatim.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_READER_ROLE = "wiki_reader"

_OLD_SIGNATURE = "api.envelope_collection(text)"
_NEW_SIGNATURE = "api.envelope_collection(text, text[])"

_NEW_BODY = """
CREATE FUNCTION api.envelope_collection(p_collection text, p_excluded_kinds text[] DEFAULT ARRAY[]::text[])
  RETURNS TABLE (id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds)
    ORDER BY p.title, p.id
$$
"""

_OLD_BODY = """
CREATE FUNCTION api.envelope_collection(p_collection text)
  RETURNS TABLE (id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
    ORDER BY p.title, p.id
$$
"""

_NEW_COMMENT = (
    "COMMENT ON FUNCTION api.envelope_collection(text, text[]) IS "
    "'Progressive disclosure, root map: the pages belonging to one collection (id, kind, title, "
    "summary), filtered only by collection and the caller-supplied excluded kinds, never by page "
    "status. Example: SELECT * FROM api.envelope_collection(''workdocs'', ''{}''::text[]);'"
)

_OLD_COMMENT = (
    "COMMENT ON FUNCTION api.envelope_collection(text) IS "
    "'Progressive disclosure, root map: the pages belonging to one collection (id, kind, title, "
    "summary), filtered only by collection, never by page status. Example: "
    "SELECT * FROM api.envelope_collection(''workdocs'');'"
)


def upgrade() -> None:
    """Replace the one-argument envelope_collection with the kind-filtering two-argument form."""
    op.execute(f"DROP FUNCTION {_OLD_SIGNATURE}")
    op.execute(_NEW_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_NEW_COMMENT)


def downgrade() -> None:
    """Restore 0012's one-argument envelope_collection verbatim."""
    op.execute(f"DROP FUNCTION {_NEW_SIGNATURE}")
    op.execute(_OLD_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_OLD_COMMENT)
