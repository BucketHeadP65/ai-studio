"""`api.search` keeps a raw chunk under `facets` or a date range only when a page made from its source passes them.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-16 00:00:00.000000

0061's `raw_keys` pool ignored `facets` and emptied once `since` or `until` was set, because a raw chunk carries
neither facets nor a created date. A search narrowed by a facet still ranked raw chunks of every source, and a dated one
ranked none. The page pool now also projects `source_id`, and `raw_keys` keeps a source when no facet and no date is
set, or when a page in the page pool was made from it. The page pool already holds only the pages that pass `facets`,
the date range and, unless `only_current` is false, currency, so a raw chunk passes exactly when such a page of its
source does. With no facet and no date, raw behaviour is unchanged.

The arguments and the result do not change, so the function is dropped and created again under the same signature,
EXECUTE is granted again and the comment, which said every raw chunk drops out under a range, is set again with how a
raw chunk passes. Downgrade restores 0061's function and 0063's comment. What this revision freezes is the descriptor,
never the rendered SQL.
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
    SearchTuning,
)

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"

_IN_RAW_KEYS = "((r.collection, r.source_id) IN (SELECT raw_keys.collection, raw_keys.source_id FROM raw_keys)) IS TRUE"
_RAW_TITLE = "(SELECT raw_keys.title FROM raw_keys WHERE raw_keys.collection = r.collection AND raw_keys.source_id = r.source_id)"

# A source passes when nothing narrows the search, or when a page in the page pool was made from it.
_SOURCED = (
    "(({facets} = '{}'::jsonb AND {since} IS NULL AND {until} IS NULL)"
    " OR ((sr.collection, sr.source_id) IN (SELECT page_pool.collection, page_pool.source_id FROM page_pool)) IS TRUE)"
)


def _in_pages(alias: str, page_col: str) -> str:
    """Whether a row's page is in the page pool, as one hashed lookup the planner never turns into a join."""
    return f"(({alias}.collection, {alias}.{page_col}) IN (SELECT page_pool.collection, page_pool.id FROM page_pool)) IS TRUE"


def _page_title(alias: str, page_col: str) -> str:
    """A row's page title, read from the page pool by key."""
    return f"(SELECT page_pool.title FROM page_pool WHERE page_pool.collection = {alias}.collection AND page_pool.id = {alias}.{page_col})"


def _search_schema(*, sourced: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `sourced=False` is 0061's shape, byte for byte. `sourced=True` lets a raw chunk pass `facets` and the date range
    through a page made from its source.

    Args:
        sourced: Whether the raw tier narrows through the pages made from each source.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """

    def outside(alias: str) -> tuple[str, ...]:
        return (f"{alias}.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",)

    model_pool = (
        FacetPool(
            name="foreign_models",
            table="kb.index_fingerprint",
            alias="f",
            select_cols=("f.collection",),
            predicates=("{query_model} IS NOT NULL", "f.embed_model <> ''", "f.embed_model <> {query_model}"),
        ),
    )
    page_cols = ("p.collection", "p.id", "p.title", "p.card_text", "p.embedding")
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
                membership=_in_pages("p", "id"),
                title_pin=True,
                partition_col="collection",
                vector_filters=outside("p"),
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
                title_expr=_page_title("s", "page_id"),
                bm25_index="sections_bm25",
                membership=_in_pages("s", "page_id"),
                partition_col="collection",
                vector_filters=outside("s"),
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
                title_expr=_page_title("c", "page_id"),
                bm25_index="claims_bm25",
                membership=_in_pages("c", "page_id"),
                filters=("(NOT {only_current} OR c.status = 'active')",),
                partition_col="collection",
                vector_filters=outside("c"),
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
                title_expr=_RAW_TITLE,
                bm25_index="raw_chunks_bm25",
                membership=_IN_RAW_KEYS,
                group_expr="'source:' || r.source_id",
                partition_col="collection",
                vector_filters=outside("r"),
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="kb.pages",
                alias="p",
                select_cols=(*page_cols, "p.source_id") if sourced else page_cols,
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                     "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                     "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                    "api.visible_collection(p.collection)",
                    "({since} IS NULL OR p.created >= {since})",
                    "({until} IS NULL OR p.created <= {until})",
                    "({include_meta} OR p.id NOT LIKE 'meta.%')",
                ),
            ),
            FacetPool(
                name="raw_keys",
                table="kb.sources_raw",
                alias="sr",
                select_cols=("sr.collection", "sr.source_id", "sr.title"),
                predicates=(
                    "({collection} IS NULL OR sr.collection = {collection})",
                    "api.visible_collection(sr.collection)",
                    _SOURCED if sourced else "({since} IS NULL AND {until} IS NULL)",
                ),
            ),
            *model_pool,
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text"),
            Facet(name="only_current", sql_type="boolean", default="true"),
            Facet(name="since", sql_type="date"),
            Facet(name="until", sql_type="date"),
            Facet(name="query_model", sql_type="text"),
            Facet(name="include_meta", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
        partition_result_col="src_collection",
        tuning=SearchTuning(relation="kb.retrieval_tuning"),
    )


def _search_body(*, sourced: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        sourced: Whether the raw tier narrows through the pages made from each source.

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
    return builder.api_search_body(_search_schema(sourced=sourced))


_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base '
    "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
    'query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller '
    'outside the doors passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model '
    'that made it. With query_model set, a knowledge base indexed under another embedding model gets no semantic '
    "leg, because its vectors are not comparable.) k caps how many hits come back (default 12, at most the owner's "
    "k ceiling, 50 unless the owner sets it lower). The 'level' arg filters to one tier (card|section|claim|raw). "
    '\'facets\' is one jsonb bag of facet name to value-list narrowing (e.g. \'{"<facet>": ["<value>"]}\'), matched '
    "against each page's own declared facets (values for one key OR'd, several keys AND'd), applied BEFORE ranking. "
    "'collection' routes the search to one knowledge base, defaulting to the reader session's own collection (the "
    'app.collection setting) rather than any fixed one. Pass collection => NULL explicitly to rank over every '
    "collection visible to the session. 'only_current' defaults to true, which ranks only over live pages and "
    "active claims. Pass false to rank over superseded pages, stubs and retired claims too. 'since' and 'until' are "
    'dates, both inclusive and both NULL by default: either one keeps only pages whose created date falls in the '
    'range, applied BEFORE ranking, so a page with no created date drops out once either is set. A raw chunk passes '
    "'facets', 'since' and 'until' only when a page made from its source passes them, a live one unless "
    "only_current is false. With none of the three set, every raw chunk ranks. 'include_meta' defaults to true. "
    "Pass false to leave out the knowledge base's own meta.* pages (orientation, policy and schema pages) before k "
    'is applied. Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, '
    'score, lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the knowledge base the '
    'hit belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on src_collection '
    "and id together. snippet is an excerpt for judging whether to fetch, cut at the owner's snippet length (480 "
    'characters unless the owner sets another, between 80 and 2,000) with a trailing ellipsis when the matched text '
    'is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole unit. After ranking, a '
    'card whose title equals the query, ignoring case and surrounding spaces, comes first when no other card holds '
    'that title, and each page (its card, sections and claims together) or source keeps at most its best few hits '
    '(3 unless the owner sets another), the pinned card outside that count. lexical_rank and vector_rank are each '
    "leg's own rank for the hit and vector_distance the semantic leg's raw distance. A NULL rank means that leg did "
    'not select this row, so the pair says which components support a hit rather than how good it is. Example: '
    "SELECT * FROM api.search('<query>', k => 8, since => '2026-09-01');"
)

# 0063's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base '
    "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
    'query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller '
    'outside the doors passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model '
    'that made it. With query_model set, a knowledge base indexed under another embedding model gets no semantic '
    "leg, because its vectors are not comparable.) k caps how many hits come back (default 12, at most the owner's "
    "k ceiling, 50 unless the owner sets it lower). The 'level' arg filters to one tier (card|section|claim|raw). "
    '\'facets\' is one jsonb bag of facet name to value-list narrowing (e.g. \'{"<facet>": ["<value>"]}\'), matched '
    "against each page's own declared facets (values for one key OR'd, several keys AND'd), applied BEFORE ranking. "
    "'collection' routes the search to one knowledge base, defaulting to the reader session's own collection (the "
    'app.collection setting) rather than any fixed one. Pass collection => NULL explicitly to rank over every '
    "collection visible to the session. 'only_current' defaults to true, which ranks only over live pages and "
    "active claims. Pass false to rank over superseded pages, stubs and retired claims too. 'since' and 'until' are "
    'dates, both inclusive and both NULL by default: either one keeps only pages whose created date falls in the '
    'range, applied BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. '
    "'include_meta' defaults to true. Pass false to leave out the knowledge base's own meta.* pages (orientation, "
    'policy and schema pages) before k is applied. Returns id, src_collection, hit_level (card|section|claim|raw), '
    'page_id, page_title, snippet, score, lexical_rank, vector_rank, vector_distance, snippet_truncated. '
    'src_collection is the knowledge base the hit belongs to. An id is unique only within its knowledge base, so '
    'join a hit to api.cards on src_collection and id together. snippet is an excerpt for judging whether to fetch, '
    "cut at the owner's snippet length (480 characters unless the owner sets another, between 80 and 2,000) with a "
    'trailing ellipsis when the matched text is longer, and snippet_truncated says whether it was cut. Fetch the id '
    'for the whole unit. After ranking, a card whose title equals the query, ignoring case and surrounding spaces, '
    'comes first when no other card holds that title, and each page (its card, sections and claims together) or '
    'source keeps at most its best few hits (3 unless the owner sets another), the pinned card outside that count. '
    "lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic leg's raw "
    'distance. A NULL rank means that leg did not select this row, so the pair says which components support a hit '
    "rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, since => '2026-09-01');"
)


def _publish_search(*, sourced: bool, comment: str) -> None:
    """Drop `api.search` and publish the shape `sourced` picks under the same signature, re-granting and commenting it."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(sourced=sourced))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {_SEARCH_SIGNATURE} IS '{comment.replace("'", "''")}'")


def upgrade() -> None:
    """Publish `api.search` with raw chunks narrowed through the pages made from their sources."""
    _publish_search(sourced=True, comment=_COMMENT_TEXT)


def downgrade() -> None:
    """Put 0061's `api.search` and 0063's comment back."""
    _publish_search(sourced=False, comment=_PREVIOUS_COMMENT_TEXT)
