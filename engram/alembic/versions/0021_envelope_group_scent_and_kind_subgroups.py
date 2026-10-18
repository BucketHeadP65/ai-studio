"""api envelope group surface: sample-title scent, and app+kind sub-groups.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-19 00:00:00.000000

Two changes to the group-counting envelope surface, the 0012/0013 replace-function idiom
throughout (`SECURITY DEFINER`, `search_path` pinned to ``memory, public, pg_catalog``, `EXECUTE`
granted to the reader role, `COMMENT ON` with a runnable example):

* ``api.envelope_collection_groups(text, text[])`` and ``api.envelope_collection_kinds(text,
  text[])`` (0019/0020) are replaced, same argument list, to add a ``sample_titles text[]``
  column: the first three page titles alphabetically in that group
  (``(array_agg(p.title ORDER BY p.title))[1:3]``), the scent a group stub renders alongside its
  page count.
* Two new functions extend `group_by="app"` one rung deeper, for when one app's own page listing
  is itself too large to list: ``api.envelope_group_kinds(p_collection text, p_app text,
  p_excluded_kinds text[])`` counts that app's pages by kind (same shape and scent as the two
  functions above, scoped to one app), and ``api.envelope_group_kind(p_collection text, p_app
  text, p_kind text, p_excluded_kinds text[])`` lists the pages belonging to that collection AND
  app AND kind — `api.envelope_group`'s own body plus `AND p.kind = p_kind`.

Downgrade drops all four current-signature functions and restores the 0019/0020 two-column
`envelope_collection_groups`/`envelope_collection_kinds` bodies verbatim; the two new functions
have no prior version to restore, so downgrade simply drops them.
"""

from __future__ import annotations

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_ENVELOPE_COLLECTION_GROUPS_SIGNATURE = "api.envelope_collection_groups(text, text[])"
_ENVELOPE_COLLECTION_KINDS_SIGNATURE = "api.envelope_collection_kinds(text, text[])"
_ENVELOPE_GROUP_KINDS_SIGNATURE = "api.envelope_group_kinds(text, text, text[])"
_ENVELOPE_GROUP_KIND_SIGNATURE = "api.envelope_group_kind(text, text, text, text[])"


def upgrade() -> None:
    """Replace the two group-counting functions with a scented form, and add the app+kind pair."""
    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE}")
    op.execute(
        """
        CREATE FUNCTION api.envelope_collection_groups(p_collection text, p_excluded_kinds text[])
          RETURNS TABLE (app text, pages bigint, sample_titles text[])
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT COALESCE(p.frontmatter ->> 'app', '') AS app,
                   count(*) AS pages,
                   (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
            FROM memory.pages p
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
            GROUP BY 1
            ORDER BY 1
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE} TO {_READER_ROLE}")

    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE}")
    op.execute(
        """
        CREATE FUNCTION api.envelope_collection_kinds(p_collection text, p_excluded_kinds text[])
          RETURNS TABLE (kind text, pages bigint, sample_titles text[])
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.kind AS kind,
                   count(*) AS pages,
                   (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
            FROM memory.pages p
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
            GROUP BY p.kind
            ORDER BY p.kind
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE} TO {_READER_ROLE}")

    op.execute(
        """
        CREATE FUNCTION api.envelope_group_kinds(p_collection text, p_app text, p_excluded_kinds text[])
          RETURNS TABLE (kind text, pages bigint, sample_titles text[])
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.kind AS kind,
                   count(*) AS pages,
                   (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
            FROM memory.pages p
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
              AND COALESCE(p.frontmatter ->> 'app', '') = p_app
            GROUP BY p.kind
            ORDER BY p.kind
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_GROUP_KINDS_SIGNATURE} TO {_READER_ROLE}")

    op.execute(
        """
        CREATE FUNCTION api.envelope_group_kind(p_collection text, p_app text, p_kind text, p_excluded_kinds text[])
          RETURNS TABLE (id text, kind text, title text, summary text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.id, p.kind, p.title,
                   COALESCE(s.summary, p.summary, '') AS summary
            FROM memory.pages p
            LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
            WHERE p.collection = p_collection
              AND p.kind <> ALL(p_excluded_kinds)
              AND COALESCE(p.frontmatter ->> 'app', '') = p_app
              AND p.kind = p_kind
            ORDER BY p.title, p.id
        $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_GROUP_KIND_SIGNATURE} TO {_READER_ROLE}")

    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop the app+kind pair, and restore the 0019/0020 two-column group-counting bodies verbatim."""
    op.execute(f"DROP FUNCTION {_ENVELOPE_GROUP_KIND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_ENVELOPE_GROUP_KINDS_SIGNATURE}")

    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE}")
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
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_KINDS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_collection_kinds(text, text[]) IS "
        "'Progressive disclosure, kind-grouped root map: one row per distinct page kind in the "
        "collection with its page count, filtered by collection and the caller-supplied excluded "
        "kinds, never by page status. Example: "
        "SELECT * FROM api.envelope_collection_kinds(''repo-docs'', ''{}''::text[]);'"
    )

    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE}")
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
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_GROUPS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_collection_groups(text, text[]) IS "
        "'Progressive disclosure, grouped root map: one row per distinct frontmatter app value in the "
        "collection (empty string = ungrouped) with its page count, filtered by collection and the "
        "caller-supplied excluded kinds, never by page status. Example: "
        "SELECT * FROM api.envelope_collection_groups(''repo-docs'', ''{}''::text[]);'"
    )


# --- COMMENTs (the M-Schema additions) ---------------------------------------
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON FUNCTION api.envelope_collection_groups(text, text[]) IS "
    "'Progressive disclosure, grouped root map: one row per distinct frontmatter app value in the "
    "collection (empty string = ungrouped) with its page count and up to 3 sample titles, filtered "
    "by collection and the caller-supplied excluded kinds, never by page status. Example: "
    "SELECT * FROM api.envelope_collection_groups(''repo-docs'', ''{}''::text[]);'",
    "COMMENT ON FUNCTION api.envelope_collection_kinds(text, text[]) IS "
    "'Progressive disclosure, kind-grouped root map: one row per distinct page kind in the "
    "collection with its page count and up to 3 sample titles, filtered by collection and the "
    "caller-supplied excluded kinds, never by page status. Example: "
    "SELECT * FROM api.envelope_collection_kinds(''repo-docs'', ''{}''::text[]);'",
    "COMMENT ON FUNCTION api.envelope_group_kinds(text, text, text[]) IS "
    "'Progressive disclosure, kind sub-groups within one app group: one row per distinct page kind "
    "belonging to one collection AND one frontmatter app value, with its page count and up to 3 "
    "sample titles, never filtered by page status. Example: SELECT * FROM "
    "api.envelope_group_kinds(''repo-docs'', ''engram'', ''{}''::text[]);'",
    "COMMENT ON FUNCTION api.envelope_group_kind(text, text, text, text[]) IS "
    "'Progressive disclosure, app+kind sub-group envelope: the pages belonging to one collection "
    "AND one frontmatter app value AND one page kind (id, kind, title, summary), same shape and "
    "exclusion as api.envelope_collection. Example: SELECT * FROM api.envelope_group_kind("
    "''repo-docs'', ''engram'', ''adr'', ''{}''::text[]);'",
)
