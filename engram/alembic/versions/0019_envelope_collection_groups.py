"""api envelope group surface: app-grouped collection maps.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-19 00:00:00.000000

A corpus spec may declare `retrieval.group_by` (currently only `"app"`): its collection map's root
level then lists app groups instead of pages, and a `group:<collection>/<app>` ref lists that
app's pages. Two functions, the 0012/0013 pattern (`SECURITY DEFINER`, `search_path` pinned to
``memory, public, pg_catalog``, `EXECUTE` granted to the reader role):

* ``api.envelope_collection_groups(p_collection text, p_excluded_kinds text[])`` — one row per
  distinct ``memory.pages.frontmatter ->> 'app'`` value in the collection (empty string for a page
  with no `app`, the ungrouped bucket), with each group's page count, filtered by `collection` and
  the corpusspec's excluded kinds exactly as `api.envelope_collection` (0013) is, never by page
  status.
* ``api.envelope_group(p_collection text, p_app text, p_excluded_kinds text[])`` — `api.envelope_collection`'s
  own body (id/kind/title/summary, same summary `COALESCE`, same exclusion, same ordering) plus
  `AND COALESCE(p.frontmatter ->> 'app', '') = p_app`, so a `group:` ref lists exactly the pages
  `api.envelope_collection_groups` counted for that app.

No index on `(collection, frontmatter ->> 'app')` in this slice — the envelope's row cap and
collection-first filtering keep the scan cheap at estate scale.

Downgrade drops both functions by signature (their grants and comments go with them).
"""

from __future__ import annotations

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_ENVELOPE_COLLECTION_GROUPS_SIGNATURE = "api.envelope_collection_groups(text, text[])"
_ENVELOPE_GROUP_SIGNATURE = "api.envelope_group(text, text, text[])"


def upgrade() -> None:
    """Create `api.envelope_collection_groups` and `api.envelope_group`, grant, and comment them."""
    op.execute(
        """
        CREATE FUNCTION api.envelope_collection_groups(p_collection text, p_excluded_kinds text[])
          RETURNS TABLE (app text, pages bigint)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT COALESCE(p.frontmatter ->> 'app', '') AS app, count(*) AS pages
            FROM memory.pages p
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
            GROUP BY 1
            ORDER BY 1
        $$
        """
    )

    op.execute(
        """
        CREATE FUNCTION api.envelope_group(p_collection text, p_app text, p_excluded_kinds text[])
          RETURNS TABLE (id text, kind text, title text, summary text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.id, p.kind, p.title,
                   COALESCE(s.summary, p.summary, '') AS summary
            FROM memory.pages p
            LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
              AND COALESCE(p.frontmatter ->> 'app', '') = p_app
            ORDER BY p.title, p.id
        $$
        """
    )

    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_GROUP_SIGNATURE} TO {_READER_ROLE}")

    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop both `api.envelope_collection_groups` and `api.envelope_group`."""
    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_ENVELOPE_GROUP_SIGNATURE}")


# --- COMMENTs (the M-Schema additions) ---------------------------------------
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON FUNCTION api.envelope_collection_groups(text, text[]) IS "
    "'Progressive disclosure, grouped root map: one row per distinct frontmatter app value in the "
    "collection (empty string = ungrouped) with its page count, filtered by collection and the "
    "caller-supplied excluded kinds, never by page status. Example: "
    "SELECT * FROM api.envelope_collection_groups(''repo-docs'', ''{}''::text[]);'",
    "COMMENT ON FUNCTION api.envelope_group(text, text, text[]) IS "
    "'Progressive disclosure, group envelope: the pages belonging to one collection AND one "
    "frontmatter app value (id, kind, title, summary), same shape and exclusion as "
    "api.envelope_collection. Example: SELECT * FROM api.envelope_group(''repo-docs'', "
    "''engram'', ''{}''::text[]);'",
)
