"""`api.search` keys every row by its collection and its id, and returns the collection as `src_collection`.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-07 00:00:00.000000

An id is unique only within its collection, yet 0049's search joined each tier to its page pool on the
id alone and fused the legs on the id alone. A search scoped to one collection returned another
collection's cards, sections, claims and raw chunks whenever that collection held the same page id or
source id, a private collection included, and two collections' rows with one id fused into one hit
with summed scores.

Every pool now projects its collection, every tier joins its pool on the collection and the id
together, the card tier's membership test compares both, and the fusion groups on both. The result
gains `src_collection` right after `id`, named as `api.cards` names a page's collection, so a caller
joins a hit to its card on `src_collection` and `id` together. The arguments are unchanged, so every
caller keeps resolving. The function is dropped and created again because its result changes, EXECUTE
is granted again by exact argument list, and the comment `describe_schema` serves is rewritten to
teach the new column. Downgrade restores 0049's function, grant and comment. What this revision
freezes is the descriptor, never the rendered SQL.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import (
    Facet,
    FacetPool,
    ParadeDBHybridSearchBuilder,
    PgTextSearchHybridSearchBuilder,
    SearchSchema,
    SearchSurface,
)

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

# The arguments do not change, so both directions drop and grant the one signature.
_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date)"


def _search_schema(*, keyed: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `keyed=False` is 0049's shape, byte for byte. `keyed=True` projects the collection from both pools,
    joins every tier to its pool on the collection and the id together, and returns the collection as
    `src_collection`.

    Args:
        keyed: Whether rows are keyed by their collection and their id.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """
    partition = "collection" if keyed else ""
    card_membership = (
        "(p.collection, p.id) IN (SELECT page_pool.collection, page_pool.id FROM page_pool)"
        if keyed
        else "p.id IN (SELECT page_pool.id FROM page_pool)"
    )
    section_join = "JOIN page_pool pp ON pp.collection = s.collection AND pp.id = s.page_id" if keyed else "JOIN page_pool pp ON pp.id = s.page_id"
    claim_join = "JOIN page_pool pp ON pp.collection = c.collection AND pp.id = c.page_id" if keyed else "JOIN page_pool pp ON pp.id = c.page_id"
    raw_join = "JOIN raw_keys rk ON rk.collection = r.collection AND rk.source_id = r.source_id" if keyed else "JOIN raw_keys rk ON rk.source_id = r.source_id"
    page_columns = ("p.collection", "p.id", "p.title", "p.card_text", "p.embedding") if keyed else ("p.id", "p.title", "p.card_text", "p.embedding")
    raw_columns = ("sr.collection", "sr.source_id", "sr.title") if keyed else ("sr.source_id", "sr.title")
    return SearchSchema(
        surfaces=(
            SearchSurface(
                name="card",
                table="kb.pages",
                alias="p",
                key_col="id",
                text_col="card_text",
                embedding_col="embedding",
                hit_level="card",
                parent_id_expr="p.id",
                title_expr="p.title",
                bm25_index="pages_bm25",
                membership=card_membership,
                partition_col=partition,
            ),
            SearchSurface(
                name="section",
                table="kb.sections",
                alias="s",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="section",
                parent_id_expr="s.page_id",
                title_expr="pp.title",
                bm25_index="sections_bm25",
                join=section_join,
                partition_col=partition,
            ),
            SearchSurface(
                name="claim",
                table="kb.claims",
                alias="c",
                key_col="id",
                text_col="text",
                embedding_col="embedding",
                hit_level="claim",
                parent_id_expr="c.page_id",
                title_expr="pp.title",
                bm25_index="claims_bm25",
                join=claim_join,
                filters=("(NOT {only_current} OR c.status = 'active')",),
                partition_col=partition,
            ),
            SearchSurface(
                name="raw",
                table="kb.raw_chunks",
                alias="r",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="raw",
                parent_id_expr="NULL::text",
                title_expr="rk.title",
                bm25_index="raw_chunks_bm25",
                join=raw_join,
                partition_col=partition,
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="kb.pages",
                alias="p",
                select_cols=page_columns,
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                     "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                     "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                    "api.visible_collection(p.collection)",
                    "({since} IS NULL OR p.created >= {since})",
                    "({until} IS NULL OR p.created <= {until})",
                ),
            ),
            FacetPool(
                name="raw_keys",
                table="kb.sources_raw",
                alias="sr",
                select_cols=raw_columns,
                predicates=(
                    "({collection} IS NULL OR sr.collection = {collection})",
                    "api.visible_collection(sr.collection)",
                    "({since} IS NULL AND {until} IS NULL)",
                ),
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text"),
            Facet(name="only_current", sql_type="boolean", default="true"),
            Facet(name="since", sql_type="date"),
            Facet(name="until", sql_type="date"),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
        partition_result_col="src_collection" if keyed else "",
    )


def _search_body(*, keyed: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        keyed: Whether rows are keyed by their collection and their id.

    Returns:
        The complete DDL statement.

    Raises:
        RuntimeError: When `alembic/env.py` stashed no recognised `SearchDialect`.
    """
    dialect_name = getattr(context.config.attributes.get("search_dialect"), "name", None)
    builder: ParadeDBHybridSearchBuilder | PgTextSearchHybridSearchBuilder
    if dialect_name == "pg_search":
        builder = ParadeDBHybridSearchBuilder()
    elif dialect_name == "pg_textsearch":
        builder = PgTextSearchHybridSearchBuilder()
    else:
        msg = f"search_dialect not configured (or unrecognised name {dialect_name!r}): alembic/env.py must stash a SearchDialect"
        raise RuntimeError(msg)
    return builder.api_search_body(_search_schema(keyed=keyed))


# Everything before the result list is 0049's comment, unchanged.
_ARGUMENTS_COMMENT = (
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "knowledge base doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds "
    "that text server-side into query_vec for you. Never build a vector by hand. Omit it for "
    "BM25-only lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec "
    "query embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' "
    "arg filters to one tier (card|section|claim|raw). ''facets'' is one jsonb bag of facet name to "
    "value-list narrowing (e.g. ''{\"tasks\": [\"retrieval\"]}''), matched against each page''s own "
    "declared facets (values for one key OR''d, several keys AND''d), applied BEFORE ranking. "
    "''collection'' routes the search to one knowledge base, defaulting to the reader session''s "
    "own collection (the app.collection setting) rather than any fixed one. Pass collection => NULL "
    "explicitly to rank over every collection visible to the session. ''only_current'' defaults to "
    "true, which ranks only over live pages and active claims. Pass false to rank over superseded "
    "pages, stubs and retired claims too. ''since'' and ''until'' are dates, both inclusive and both "
    "NULL by default: either one keeps only pages whose created date falls in the range, applied "
    "BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. "
)

_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean, date, date) IS "
    + _ARGUMENTS_COMMENT
    + "Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, "
    "score, lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the "
    "knowledge base the hit belongs to. An id is unique only within its knowledge base, so join a hit "
    "to api.cards on src_collection and id together. snippet is an excerpt for "
    "judging whether to fetch, cut at 480 characters with a trailing ellipsis when the matched text "
    "is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole unit. "
    "lexical_rank and vector_rank are each leg''s own rank for the hit and vector_distance the "
    "semantic leg''s raw distance. A NULL rank means that leg did not select this row, so the pair "
    "says which components support a hit rather than how good it is. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, since => ''2026-09-01'');'"
)

# 0049's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean, date, date) IS "
    + _ARGUMENTS_COMMENT
    + "Returns id, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, "
    "lexical_rank, vector_rank, vector_distance, snippet_truncated. snippet is an excerpt for "
    "judging whether to fetch, cut at 480 characters with a trailing ellipsis when the matched text "
    "is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole unit. "
    "lexical_rank and vector_rank are each leg''s own rank for the hit and vector_distance the "
    "semantic leg''s raw distance. A NULL rank means that leg did not select this row, so the pair "
    "says which components support a hit rather than how good it is. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, since => ''2026-09-01'');'"
)


def _publish_search(*, keyed: bool, comment: str) -> None:
    """Drop `api.search` and publish the shape `keyed` picks, re-granting by exact signature."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(keyed=keyed))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(comment)


def upgrade() -> None:
    """Republish `api.search` keyed by collection and id, returning `src_collection`."""
    _publish_search(keyed=True, comment=_SEARCH_COMMENT)


def downgrade() -> None:
    """Put 0049's `api.search` and its comment back."""
    _publish_search(keyed=False, comment=_PREVIOUS_SEARCH_COMMENT)
