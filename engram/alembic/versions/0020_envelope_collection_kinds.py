"""api envelope kind surface: kind-grouped collection maps.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-19 00:00:00.000000

A corpus spec may now declare `retrieval.group_by = "kind"` alongside the existing `"app"`
(0019): its collection map's root level then lists kind groups instead of pages, and a
`group:<collection>/<kind>` ref lists that kind's pages. Two functions, the same 0019/0012/0013
pattern (`SECURITY DEFINER`, `search_path` pinned to ``memory, public, pg_catalog``, `EXECUTE`
granted to the reader role):

* ``api.envelope_collection_kinds(p_collection text, p_excluded_kinds text[])`` — one row per
  distinct ``memory.pages.kind`` value in the collection, with each kind's page count, filtered by
  `collection` and the corpusspec's excluded kinds exactly as `api.envelope_collection` (0013) is,
  never by page status. Unlike the `"app"` grouping's frontmatter lookup, `kind` is a first-class
  `NOT NULL` column, so there is no ungrouped bucket to COALESCE.
* ``api.envelope_kind(p_collection text, p_kind text, p_excluded_kinds text[])`` — `api.envelope_collection`'s
  own body (id/kind/title/summary, same summary `COALESCE`, same exclusion, same ordering) plus
  `AND p.kind = p_kind`, so a `group:` ref lists exactly the pages `api.envelope_collection_kinds`
  counted for that kind.

No index on `(collection, kind)` in this slice, same rationale as 0019 — the envelope's row cap and
collection-first filtering keep the scan cheap at estate scale.

Downgrade drops both functions by signature (their grants and comments go with them).
"""

from __future__ import annotations

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_ENVELOPE_COLLECTION_KINDS_SIGNATURE = "api.envelope_collection_kinds(text, text[])"
_ENVELOPE_KIND_SIGNATURE = "api.envelope_kind(text, text, text[])"


def upgrade() -> None:
    """Create `api.envelope_collection_kinds` and `api.envelope_kind`, grant, and comment them."""
    op.execute(
        """
        CREATE FUNCTION api.envelope_collection_kinds(p_collection text, p_excluded_kinds text[])
          RETURNS TABLE (kind text, pages bigint)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.kind AS kind, count(*) AS pages
            FROM memory.pages p
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
            GROUP BY p.kind
            ORDER BY p.kind
        $$
        """
    )

    op.execute(
        """
        CREATE FUNCTION api.envelope_kind(p_collection text, p_kind text, p_excluded_kinds text[])
          RETURNS TABLE (id text, kind text, title text, summary text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.id, p.kind, p.title,
                   COALESCE(s.summary, p.summary, '') AS summary
            FROM memory.pages p
            LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
              AND p.kind = p_kind
            ORDER BY p.title, p.id
        $$
        """
    )

    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_KIND_SIGNATURE} TO {_READER_ROLE}")

    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop both `api.envelope_collection_kinds` and `api.envelope_kind`."""
    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_ENVELOPE_KIND_SIGNATURE}")


# --- COMMENTs (the M-Schema additions) ---------------------------------------
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON FUNCTION api.envelope_collection_kinds(text, text[]) IS "
    "'Progressive disclosure, kind-grouped root map: one row per distinct page kind in the "
    "collection with its page count, filtered by collection and the caller-supplied excluded "
    "kinds, never by page status. Example: "
    "SELECT * FROM api.envelope_collection_kinds(''repo-docs'', ''{}''::text[]);'",
    "COMMENT ON FUNCTION api.envelope_kind(text, text, text[]) IS "
    "'Progressive disclosure, kind-group envelope: the pages belonging to one collection AND one "
    "page kind (id, kind, title, summary), same shape and exclusion as api.envelope_collection. "
    "Example: SELECT * FROM api.envelope_kind(''repo-docs'', ''adr'', ''{}''::text[]);'",
)
