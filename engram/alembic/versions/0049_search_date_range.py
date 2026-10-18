"""`api.search` takes a date range: `since` and `until` keep pages whose `created` date falls in it.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-06 00:00:00.000000

`api.search` gains two trailing arguments, `since date DEFAULT NULL` and `until date DEFAULT NULL`.
Both bounds are inclusive and both are applied in the page pool before ranking, so a search narrowed
to a range never spends its `k` rows on pages outside it. Sections and claims reach their page
through that pool and follow it. A page with no `created` date never matches a range, and the raw
tier, whose chunks carry no date at all, answers nothing once either bound is set. With both left
NULL the function ranks exactly as 0048's did.

The seven-argument function is dropped before the nine-argument one is created, so no overload is
left behind for a call to resolve against. Every existing caller names its arguments or passes the
first three by position, so each call keeps resolving. EXECUTE is granted again by exact argument
list after each drop, and the comment `describe_schema` serves is rewritten to teach the range.
Downgrade restores 0048's function, grant and comment. What this revision freezes is the
descriptor, never the rendered SQL.
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

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

# Every DROP and GRANT names an exact argument list, so the date range never leaves a second overload behind.
_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date)"
_PREVIOUS_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean)"


def _search_schema(*, dated: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `dated=False` is 0048's shape, byte for byte. `dated=True` adds the `since` and `until` facets, two
    page-pool predicates that compare them with `p.created`, and a raw-key predicate that empties the raw
    tier once either is set.

    Args:
        dated: Whether the shape carries the date range.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """
    page_dates = ("({since} IS NULL OR p.created >= {since})", "({until} IS NULL OR p.created <= {until})") if dated else ()
    raw_dates = ("({since} IS NULL AND {until} IS NULL)",) if dated else ()
    date_facets = (Facet(name="since", sql_type="date"), Facet(name="until", sql_type="date")) if dated else ()
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
                membership="p.id IN (SELECT page_pool.id FROM page_pool)",
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
                join="JOIN page_pool pp ON pp.id = s.page_id",
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
                join="JOIN page_pool pp ON pp.id = c.page_id",
                filters=("(NOT {only_current} OR c.status = 'active')",),
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
                join="JOIN raw_keys rk ON rk.source_id = r.source_id",
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="kb.pages",
                alias="p",
                select_cols=("p.id", "p.title", "p.card_text", "p.embedding"),
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                     "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                     "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                    "api.visible_collection(p.collection)",
                    *page_dates,
                ),
            ),
            FacetPool(
                name="raw_keys",
                table="kb.sources_raw",
                alias="sr",
                select_cols=("sr.source_id", "sr.title"),
                predicates=(
                    "({collection} IS NULL OR sr.collection = {collection})",
                    "api.visible_collection(sr.collection)",
                    *raw_dates,
                ),
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text"),
            Facet(name="only_current", sql_type="boolean", default="true"),
            *date_facets,
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
    )


def _search_body(*, dated: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        dated: Whether the function takes the date range.

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
    return builder.api_search_body(_search_schema(dated=dated))


_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean, date, date) IS "
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
    "Returns id, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, "
    "lexical_rank, vector_rank, vector_distance, snippet_truncated. snippet is an excerpt for "
    "judging whether to fetch, cut at 480 characters with a trailing ellipsis when the matched text "
    "is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole unit. "
    "lexical_rank and vector_rank are each leg''s own rank for the hit and vector_distance the "
    "semantic leg''s raw distance. A NULL rank means that leg did not select this row, so the pair "
    "says which components support a hit rather than how good it is. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, since => ''2026-09-01'');'"
)

# 0048's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "knowledge base doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds "
    "that text server-side into query_vec for you — never build a vector by hand; omit it for "
    "BM25-only lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec "
    "query embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' "
    "arg filters to one tier (card|section|claim|raw); ''facets'' is one jsonb bag of facet name to "
    "value-list narrowing (e.g. ''{\"tasks\": [\"retrieval\"]}''), matched against each page''s own "
    "declared facets — values for one key OR''d, several keys AND''d — applied BEFORE ranking; "
    "''collection'' routes the search to one knowledge base, defaulting to the reader session''s "
    "own collection (the app.collection setting) rather than any fixed one; pass collection => NULL "
    "explicitly to rank over every collection visible to the session; ''only_current'' defaults to "
    "true, which ranks only over live pages and active claims — pass false to rank over superseded "
    "pages, stubs and retired claims too. Returns id, hit_level (card|section|claim|raw), page_id, "
    "page_title, snippet, score, lexical_rank, vector_rank, vector_distance, snippet_truncated. "
    "snippet is an excerpt for judging whether to fetch, cut at 480 characters with a trailing "
    "ellipsis when the matched text is longer, and snippet_truncated says whether it was cut; fetch "
    "the id for the whole unit. lexical_rank and vector_rank are each leg''s own rank for the hit "
    "and vector_distance the semantic leg''s raw distance — a NULL rank means that leg did not "
    "select this row, so the pair says which components support a hit rather than how good it is. "
    "Example: SELECT * FROM api.search(''graph rag token cost'', k => 8, facets => "
    "''{\"tasks\": [\"retrieval\"]}'');'"
)


def _publish_search(*, dated: bool, signature: str, comment: str, replacing: str) -> None:
    """Drop the `api.search` overload `replacing` names and publish the shape `dated` picks, re-granting by exact signature."""
    op.execute(f"DROP FUNCTION {replacing}")
    op.execute(_search_body(dated=dated))
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_READER_ROLE}")
    op.execute(comment)


def upgrade() -> None:
    """Republish `api.search` with its `since` and `until` arguments."""
    _publish_search(dated=True, signature=_SEARCH_SIGNATURE, comment=_SEARCH_COMMENT, replacing=_PREVIOUS_SEARCH_SIGNATURE)


def downgrade() -> None:
    """Put 0048's seven-argument `api.search` and its comment back."""
    _publish_search(dated=False, signature=_PREVIOUS_SEARCH_SIGNATURE, comment=_PREVIOUS_SEARCH_COMMENT, replacing=_SEARCH_SIGNATURE)
