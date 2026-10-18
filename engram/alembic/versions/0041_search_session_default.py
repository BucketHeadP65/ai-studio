"""`api.search`'s `collection` argument defaults to the reader session's own, never a literal corpus name.

Revision ID: 0041
Revises: 0039
Create Date: 2026-09-01 00:00:00.000000

An omitted `collection` argument used to rank over the `research` collection specifically — the
right default only for whichever corpus happened to compile first. It now defaults to
`current_setting('app.collection', true)`, the same session setting `api.visible_collection` reads
(0037): a caller who omits `collection` gets their own session's collection, exactly as if they had
passed it explicitly. Passing `collection => NULL` explicitly is unchanged: the pool predicates
still read `({collection} IS NULL OR ...)`, so an explicit NULL still ranks over every collection
visible to the session, and a session with no `app.collection` set falls into that same case (the
setting itself reads NULL) rather than resolving to any one collection.

The generated shape is otherwise byte-identical to 0037's guarded body: same surfaces, pools, and
predicates. Only the builder's rendering of the `collection` facet's `DEFAULT` clause changes
(`core.ai.retrieval.search_builder.BaseHybridSearchBuilder._facet_default`), so this revision only
needs to drop and recreate `api.search` through the same builders, and rewrite its comment.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import SearchSchema, Facet, FacetPool, ParadeDBHybridSearchBuilder, PgTextSearchHybridSearchBuilder, SearchSurface

revision: str = "0041"
down_revision: str | None = "0039"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean)"


def _search_schema(*, collection_default: str | None) -> SearchSchema:
    """The hybrid-search shape `api.search` carries as of 0037, unchanged here.

    Migration-local, the convention every revision that regenerates `api.search` follows (0037):
    a frozen snapshot of the shape at this revision, so this revision changes only what the builder
    renders for the `collection` facet's `DEFAULT`, nothing else about the function.

    Args:
        collection_default: `None` for the `upgrade` path: the schema declares `collection` as its
            `collection_facet`, so the builder renders the session setting regardless of this
            facet's own `Facet.default`. A literal SQL default (`"'research'"`) for the `downgrade`
            path: the schema leaves `collection_facet` unset, so the builder falls back to
            `Facet.default` verbatim, restoring 0037's original rendering.

    Returns:
        The corpus schema the configured dialect's builder generates `api.search` from.
    """
    page_predicates = (
        "({collection} IS NULL OR p.collection = {collection})",
        ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
         "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
         "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
        "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
        "api.visible_collection(p.collection)",
    )
    raw_predicates = (
        "({collection} IS NULL OR sr.collection = {collection})",
        "api.visible_collection(sr.collection)",
    )
    return SearchSchema(
        surfaces=(
            SearchSurface(
                name="card",
                table="memory.pages",
                alias="p",
                key_col="id",
                text_col="card_text",
                embedding_col="embedding",
                hit_level="card",
                parent_id_expr="p.id",
                title_expr="p.title",
                bm25_index="pages_bm25",
                membership="p.id IN (SELECT page_pool.id FROM page_pool)",
            ),
            SearchSurface(
                name="section",
                table="memory.sections",
                alias="s",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="section",
                parent_id_expr="s.page_id",
                title_expr="pp.title",
                bm25_index="sections_bm25",
                join="JOIN page_pool pp ON pp.id = s.page_id",
            ),
            SearchSurface(
                name="claim",
                table="memory.claims",
                alias="c",
                key_col="id",
                text_col="text",
                embedding_col="embedding",
                hit_level="claim",
                parent_id_expr="c.page_id",
                title_expr="pp.title",
                bm25_index="claims_bm25",
                join="JOIN page_pool pp ON pp.id = c.page_id",
                filters=("(NOT {only_current} OR c.status = 'active')",),
            ),
            SearchSurface(
                name="raw",
                table="memory.raw_chunks",
                alias="r",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="raw",
                parent_id_expr="NULL::text",
                title_expr="rk.title",
                bm25_index="raw_chunks_bm25",
                join="JOIN raw_keys rk ON rk.source_id = r.source_id",
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="memory.pages",
                alias="p",
                select_cols=("p.id", "p.title", "p.card_text", "p.embedding"),
                predicates=page_predicates,
            ),
            FacetPool(
                name="raw_keys",
                table="memory.sources_raw",
                alias="sr",
                select_cols=("sr.source_id", "sr.title"),
                predicates=raw_predicates,
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text", default=collection_default),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        collection_facet="collection" if collection_default is None else "",
        embedding_sql_type="halfvec(1024)",
    )


def _search_body(*, collection_default: str | None) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar."""
    dialect_name = getattr(context.config.attributes.get("search_dialect"), "name", None)
    builder: ParadeDBHybridSearchBuilder | PgTextSearchHybridSearchBuilder
    if dialect_name == "pg_search":
        builder = ParadeDBHybridSearchBuilder()
    elif dialect_name == "pg_textsearch":
        builder = PgTextSearchHybridSearchBuilder()
    else:
        msg = f"search_dialect not configured (or unrecognised name {dialect_name!r}): alembic/env.py must stash a SearchDialect"
        raise RuntimeError(msg)
    return builder.api_search_body(_search_schema(collection_default=collection_default))


_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "corpus doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds that "
    "text server-side into query_vec for you — never build a vector by hand; omit it for BM25-only "
    "lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec query "
    "embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' arg "
    "filters to one tier (card|section|claim|raw); ''facets'' is one jsonb bag of facet name to "
    "value-list narrowing (e.g. ''{\"tasks\": [\"retrieval\"]}''), matched against each page''s own "
    "declared facets — values for one key OR''d, several keys AND''d — applied BEFORE ranking; "
    "''collection'' routes the search to one corpus, defaulting to the reader session''s own "
    "collection (the app.collection setting) rather than any fixed corpus; pass collection => NULL "
    "explicitly to rank over every collection visible to the session. Returns id, hit_level "
    "(card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, facets => ''{\"tasks\": [\"retrieval\"]}'');'"
)

# 0037's comment, byte-identical: what `downgrade` restores.
_OLD_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "corpus doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds that "
    "text server-side into query_vec for you — never build a vector by hand; omit it for BM25-only "
    "lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec query "
    "embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' arg "
    "filters to one tier (card|section|claim|raw); ''facets'' is one jsonb bag of facet name to "
    "value-list narrowing (e.g. ''{\"tasks\": [\"retrieval\"]}''), matched against each page''s own "
    "declared facets — values for one key OR''d, several keys AND''d — applied BEFORE ranking; "
    "''collection'' routes the search to one corpus (default ''research''). Returns id, hit_level "
    "(card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, facets => ''{\"tasks\": [\"retrieval\"]}'');'"
)


def upgrade() -> None:
    """Regenerate `api.search` so an omitted `collection` argument defaults to the session's own collection."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(collection_default=None))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_SEARCH_COMMENT)


def downgrade() -> None:
    """Restore `api.search`'s literal `'research'` default for an omitted `collection` argument."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(collection_default="'research'"))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_OLD_SEARCH_COMMENT)
