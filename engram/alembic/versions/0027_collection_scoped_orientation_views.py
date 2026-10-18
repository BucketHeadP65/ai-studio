"""The legacy orientation surface becomes collection-honest: api.about/schema_guide/authoring_rules.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-23 00:00:01.000000

`api.about` and `api.authoring_rules` (migrations 0002, 0008) have always read a bare literal id --
`id = 'meta.about'` / `id = 'meta.authoring'`, with no `collection` filter at all. Since migration
0023, `engram.compile.handlers.orientation_page_id` keeps that bare literal for exactly one
collection (`research`) and scopes every other collection's orientation page to
`f"{base_id}@{collection}"` -- so on a database holding several collections, these two views answer
with `research`'s orientation text alone, silently, whatever collection an agent actually means.
`api.schema_guide` has the same disease by a different route: it reads every `memory.vocab` row
tagged `vocab = 'schema_guide'` with no `collection` filter either, so a multi-collection database
returns every collection's rows conflated in one unlabeled result.

This revision makes the three views collection-honest without breaking a single-corpus caller:
each gains a leading `collection` column and now returns one row *per collection* that has
compiled the surface, rather than one unscoped row (or an unlabeled mix) for the whole database.
`api.about` / `api.authoring_rules` derive each row's expected id from its own `collection` column
with the same bare-vs-`@collection` construction `orientation_page_id` writes -- reading the
authoritative `memory.pages.collection` column directly rather than re-parsing it back out of the
id string, so a collection name that happened to contain `@` could never desync the two. A database
with one collection still gets exactly one row from each view, byte-identical to what it read
before this revision shipped; a multi-collection database now sees every collection labeled instead
of only `research`'s.

Two new functions -- `api.collection_schema_guide(p_collection)` and
`api.collection_authoring_rules(p_collection)` -- give a caller who wants one collection's rows
without a `WHERE` clause of their own the same scoped-lookup shape `api.collection_about` (0018,
0023) already established; each view's `COMMENT` now points there. `api.collection_authoring_rules`
mirrors `api.collection_about`'s two-part guard (the derived id AND `p.collection = p_collection`)
byte-for-byte; `api.collection_schema_guide` needs only the `collection` equality, since
`memory.vocab` rows carry their own collection natively and were never id-scheme-encoded.

Downgrade restores the three views' pre-0027 single-collection, unlabeled bodies exactly (0002's
`about`/`schema_guide`, 0008's `authoring_rules`) and drops the two new functions.
"""

from __future__ import annotations

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_COLLECTION_SCHEMA_GUIDE_SIGNATURE = "api.collection_schema_guide(text)"
_COLLECTION_AUTHORING_RULES_SIGNATURE = "api.collection_authoring_rules(text)"

# -- the two new scoped functions, mirroring api.collection_about ------------

_COLLECTION_SCHEMA_GUIDE_BODY = """
CREATE FUNCTION api.collection_schema_guide(p_collection text)
  RETURNS TABLE (surface text, purpose text, notes jsonb)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value AS surface, v.definition AS purpose, v.meta AS notes
    FROM memory.vocab v
    WHERE v.vocab = 'schema_guide' AND v.collection = p_collection
$$
"""

_COLLECTION_SCHEMA_GUIDE_COMMENT = (
    "COMMENT ON FUNCTION api.collection_schema_guide(text) IS "
    "'One collection''s schema guide: its own api-surface documentation rows (surface, purpose, "
    "notes), scoped to one corpus''s vocab. Zero rows when the collection wrote none. Example: "
    "SELECT * FROM api.collection_schema_guide(''research'');'"
)

_COLLECTION_AUTHORING_RULES_BODY = """
CREATE FUNCTION api.collection_authoring_rules(p_collection text)
  RETURNS TABLE (body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p_collection END)
      AND p.collection = p_collection
$$
"""

_COLLECTION_AUTHORING_RULES_COMMENT = (
    "COMMENT ON FUNCTION api.collection_authoring_rules(text) IS "
    "'One collection''s authoring contract (its compiled meta.authoring page): frontmatter keys, "
    "page types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, "
    "lint -- scoped to one corpus. Zero rows when the collection declared no authoring document. "
    "Example: SELECT body FROM api.collection_authoring_rules(''research'');'"
)


def _create_collection_functions() -> None:
    """Create the two scoped functions and grant/comment them like `api.collection_about`."""
    op.execute(_COLLECTION_SCHEMA_GUIDE_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_COLLECTION_SCHEMA_GUIDE_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_COLLECTION_SCHEMA_GUIDE_COMMENT)

    op.execute(_COLLECTION_AUTHORING_RULES_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_COLLECTION_AUTHORING_RULES_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_COLLECTION_AUTHORING_RULES_COMMENT)


def _drop_collection_functions() -> None:
    """Downgrade mirror of `_create_collection_functions`."""
    op.execute(f"DROP FUNCTION {_COLLECTION_AUTHORING_RULES_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_COLLECTION_SCHEMA_GUIDE_SIGNATURE}")


# -- the three legacy views, redefined collection-honest ----------------------

_NEW_ABOUT_VIEW = """
CREATE VIEW api.about AS
SELECT
  p.collection,
  p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p.collection END)
"""

_OLD_ABOUT_VIEW = """
CREATE VIEW api.about AS
SELECT card_text AS body FROM memory.pages WHERE id = 'meta.about'
"""

_NEW_ABOUT_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.about IS 'L0 orientation for QUERYING -- one row per collection compiled "
    "into this database: what it covers, what it excludes, and its retrieval policy (how to route "
    "a question). Read once per session before querying. A single-corpus database returns one row; "
    "a multi-corpus database returns one per collection -- filter by collection, or call "
    "api.collection_about(p_collection) for just yours. Does NOT contain the authoring/ingestion "
    "contract -- that is api.authoring_rules. Example: SELECT body FROM api.about WHERE collection "
    "= ''research'';'",
    "COMMENT ON COLUMN api.about.collection IS 'The collection this orientation row belongs to. "
    "Example value: ''research''.'",
    "COMMENT ON COLUMN api.about.body IS 'The orientation prose (markdown). Example value: ''This "
    "corpus indexes RAG/retrieval literature; start at api.cards, then api.search…''.'",
)

# The exact texts 0015 left live (0002's original wording, re-issued with corpus/memory vocabulary),
# restored on downgrade.
_OLD_ABOUT_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.about IS 'L0 orientation for QUERYING this corpus — one row: what it covers, what it "
    "excludes, and its retrieval policy (how to route a question). Read once per session before querying. Does NOT "
    "contain the authoring/ingestion contract — that is api.authoring_rules. Example: SELECT body FROM api.about;'",
    "COMMENT ON COLUMN api.about.body IS 'The orientation prose (markdown). Example value: ''This corpus indexes "
    "RAG/retrieval literature; start at api.cards, then api.search…''.'",
)

_NEW_SCHEMA_GUIDE_VIEW = """
CREATE VIEW api.schema_guide AS
SELECT
  v.collection,
  v.value AS surface,
  v.definition AS purpose,
  v.meta AS notes
FROM memory.vocab v
WHERE v.vocab = 'schema_guide'
"""

_OLD_SCHEMA_GUIDE_VIEW = """
CREATE VIEW api.schema_guide AS
SELECT value AS surface, definition AS purpose, meta AS notes
FROM memory.vocab WHERE vocab = 'schema_guide'
"""

_NEW_SCHEMA_GUIDE_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.schema_guide IS 'One row per (collection, api surface): its purpose and "
    "1-3 exemplar queries, the map of this schema. A single-corpus database returns one row per "
    "surface; a multi-corpus database returns one per collection too -- filter by collection, or "
    "call api.collection_schema_guide(p_collection) for just yours. Example: SELECT surface, "
    "purpose FROM api.schema_guide WHERE collection = ''research'';'",
    "COMMENT ON COLUMN api.schema_guide.collection IS 'The collection this schema-guide row belongs "
    "to. Example value: ''research''.'",
    "COMMENT ON COLUMN api.schema_guide.surface IS 'The api object name. Example value: "
    "''api.search''.'",
    "COMMENT ON COLUMN api.schema_guide.purpose IS 'What the surface is for, in one sentence.'",
    "COMMENT ON COLUMN api.schema_guide.notes IS 'jsonb: column notes + exemplar queries for the surface.'",
)

# 0002's original texts -- never touched by 0015 (they carry no "vault"/"wiki" vocabulary) --
# restored on downgrade.
_OLD_SCHEMA_GUIDE_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.schema_guide IS 'One row per api surface: its purpose and 1-3 exemplar queries. The map of this schema. Example: SELECT surface, purpose FROM api.schema_guide;'",
    "COMMENT ON COLUMN api.schema_guide.surface IS 'The api object name. Example value: ''api.search''.'",
    "COMMENT ON COLUMN api.schema_guide.purpose IS 'What the surface is for, in one sentence.'",
    "COMMENT ON COLUMN api.schema_guide.notes IS 'jsonb: column notes + exemplar queries for the surface.'",
)

_NEW_AUTHORING_RULES_VIEW = """
CREATE VIEW api.authoring_rules AS
SELECT
  p.collection,
  p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p.collection END)
"""

_OLD_AUTHORING_RULES_VIEW = """
CREATE VIEW api.authoring_rules AS
SELECT card_text AS body FROM memory.pages WHERE id = 'meta.authoring'
"""

_NEW_AUTHORING_RULES_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.authoring_rules IS 'The contract for WRITING pages, one row per collection "
    "compiled into this database: frontmatter keys, page types, canonical sections, claims grammar, "
    "typed relations, lifecycle, ingest procedure, lint. Read this only when authoring or ingesting "
    "-- a querying agent needs api.about instead. A single-corpus database returns one row; a "
    "multi-corpus database returns one per collection -- filter by collection, or call "
    "api.collection_authoring_rules(p_collection) for just yours. Empty for a collection that "
    "declares no authoring document. Example: SELECT body FROM api.authoring_rules WHERE "
    "collection = ''research'';'",
    "COMMENT ON COLUMN api.authoring_rules.collection IS 'The collection this authoring-contract "
    "row belongs to. Example value: ''research''.'",
    "COMMENT ON COLUMN api.authoring_rules.body IS 'The authoring contract (markdown). Example "
    "value: ''# Corpus schema — the contract … ## Frontmatter contract''.'",
)

# The exact texts 0015 left live, restored on downgrade.
_OLD_AUTHORING_RULES_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.authoring_rules IS 'The contract for WRITING pages in this corpus: frontmatter keys, page "
    "types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, lint. Read this only "
    "when authoring or ingesting — a querying agent needs api.about instead. Empty when the corpus declares no "
    "authoring document. Example: SELECT body FROM api.authoring_rules;'",
    "COMMENT ON COLUMN api.authoring_rules.body IS 'The authoring contract (markdown). Example value: ''# Corpus "
    "schema — the contract … ## Frontmatter contract''.'",
)

_VIEWS_NEW: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (_NEW_ABOUT_VIEW, "about", _NEW_ABOUT_COMMENTS),
    (_NEW_SCHEMA_GUIDE_VIEW, "schema_guide", _NEW_SCHEMA_GUIDE_COMMENTS),
    (_NEW_AUTHORING_RULES_VIEW, "authoring_rules", _NEW_AUTHORING_RULES_COMMENTS),
)

_VIEWS_OLD: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (_OLD_ABOUT_VIEW, "about", _OLD_ABOUT_COMMENTS),
    (_OLD_SCHEMA_GUIDE_VIEW, "schema_guide", _OLD_SCHEMA_GUIDE_COMMENTS),
    (_OLD_AUTHORING_RULES_VIEW, "authoring_rules", _OLD_AUTHORING_RULES_COMMENTS),
)


def _redefine_views(views: tuple[tuple[str, str, tuple[str, ...]], ...]) -> None:
    """DROP + CREATE each `(view_sql, view_name, comments)` triple, re-granting SELECT and re-commenting."""
    for view_sql, view_name, comments in views:
        op.execute(f"DROP VIEW api.{view_name}")
        op.execute(view_sql)
        op.execute(f"GRANT SELECT ON api.{view_name} TO {_READER_ROLE}")
        for statement in comments:
            op.execute(statement)


def upgrade() -> None:
    """Add the two scoped functions, then redefine the three legacy views collection-honest."""
    _create_collection_functions()
    _redefine_views(_VIEWS_NEW)


def downgrade() -> None:
    """Restore the three views' pre-0027 single-collection bodies, then drop the two scoped functions."""
    _redefine_views(_VIEWS_OLD)
    _drop_collection_functions()
