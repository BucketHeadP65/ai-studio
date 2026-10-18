"""`api.search` leaves out a knowledge base's own `meta.*` pages before it cuts to `k`, when asked.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-10 00:00:00.000000

The search tool dropped `meta.*` pages after `api.search` had already cut its result to `k`, so a search whose best
matches were orientation or policy pages answered fewer than `k` rows, or none, while saying it was complete. The same
outer filter compared each hit's `page_id`, which a raw chunk does not have, so no raw hit ever reached the tool.

`api.search` gains a trailing `include_meta boolean DEFAULT true`. False, the page pool leaves out every page whose id
starts with `meta.`, so its cards, sections and claims never rank and `k` counts only what the caller keeps. True, the
default, ranks exactly as 0057's did, so `run_sql` still reads the meta pages. The ten-argument function is dropped
before the eleven-argument one is created, EXECUTE is granted again by exact argument list, and the comment names the
new argument. Downgrade restores 0057's function, grant and comment. What this revision freezes is the descriptor,
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
)

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"
_PREVIOUS_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text)"


def _search_schema(*, meta_aware: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `meta_aware=False` is 0057's shape, byte for byte. `meta_aware=True` adds the `include_meta` facet and a page-pool
    predicate that leaves out every `meta.*` page when it is false.

    Args:
        meta_aware: Whether the function can leave out the `meta.*` pages.

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
                membership="(p.collection, p.id) IN (SELECT page_pool.collection, page_pool.id FROM page_pool)",
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
                title_expr="pp.title",
                bm25_index="sections_bm25",
                join="JOIN page_pool pp ON pp.collection = s.collection AND pp.id = s.page_id",
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
                title_expr="pp.title",
                bm25_index="claims_bm25",
                join="JOIN page_pool pp ON pp.collection = c.collection AND pp.id = c.page_id",
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
                title_expr="rk.title",
                bm25_index="raw_chunks_bm25",
                join="JOIN raw_keys rk ON rk.collection = r.collection AND rk.source_id = r.source_id",
                partition_col="collection",
                vector_filters=outside("r"),
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="kb.pages",
                alias="p",
                select_cols=("p.collection", "p.id", "p.title", "p.card_text", "p.embedding"),
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                     "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                     "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                    "api.visible_collection(p.collection)",
                    "({since} IS NULL OR p.created >= {since})",
                    "({until} IS NULL OR p.created <= {until})",
                    *(("({include_meta} OR p.id NOT LIKE 'meta.%')",) if meta_aware else ()),
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
                    "({since} IS NULL AND {until} IS NULL)",
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
            *((Facet(name="include_meta", sql_type="boolean", default="true"),) if meta_aware else ()),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
        partition_result_col="src_collection",
    )


def _search_body(*, meta_aware: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        meta_aware: Whether the function can leave out the `meta.*` pages.

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
    return builder.api_search_body(_search_schema(meta_aware=meta_aware))


# 0057's comment, byte-identical: what `downgrade` restores, and what `upgrade` extends.
_PREVIOUS_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base '
    "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
    'query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller '
    'outside the doors passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model '
    'that made it. With query_model set, a knowledge base indexed under another embedding model gets no semantic '
    "leg, because its vectors are not comparable.) k caps how many hits come back (default 12, max 50). The 'level' "
    "arg filters to one tier (card|section|claim|raw). 'facets' is one jsonb bag of facet name to value-list "
    'narrowing (e.g. \'{"<facet>": ["<value>"]}\'), matched against each page\'s own declared facets (values for one '
    "key OR'd, several keys AND'd), applied BEFORE ranking. 'collection' routes the search to one knowledge base, "
    "defaulting to the reader session's own collection (the app.collection setting) rather than any fixed one. Pass "
    "collection => NULL explicitly to rank over every collection visible to the session. 'only_current' defaults to "
    'true, which ranks only over live pages and active claims. Pass false to rank over superseded pages, stubs and '
    "retired claims too. 'since' and 'until' are dates, both inclusive and both NULL by default: either one keeps "
    'only pages whose created date falls in the range, applied BEFORE ranking, so a page with no created date and '
    'every raw chunk drop out once either is set. Returns id, src_collection, hit_level (card|section|claim|raw), '
    'page_id, page_title, snippet, score, lexical_rank, vector_rank, vector_distance, snippet_truncated. '
    'src_collection is the knowledge base the hit belongs to. An id is unique only within its knowledge base, so '
    'join a hit to api.cards on src_collection and id together. snippet is an excerpt for judging whether to fetch, '
    'cut at 480 characters with a trailing ellipsis when the matched text is longer, and snippet_truncated says '
    "whether it was cut. Fetch the id for the whole unit. lexical_rank and vector_rank are each leg's own rank for "
    "the hit and vector_distance the semantic leg's raw distance. A NULL rank means that leg did not select this "
    'row, so the pair says which components support a hit rather than how good it is. Example: SELECT * FROM '
    "api.search('<query>', k => 8, since => '2026-09-01');"
)

_COMMENT_TEXT = _PREVIOUS_COMMENT_TEXT.replace(
    'applied BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. ',
    'applied BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. '
    "'include_meta' defaults to true. Pass false to leave out the knowledge base's own meta.* pages (orientation, "
    "policy and schema pages) before k is applied. ",
)


def _comment(signature: str, body: str) -> str:
    """The `COMMENT ON FUNCTION` statement for `signature`, with `body` quoted."""
    return f"COMMENT ON FUNCTION {signature} IS '{body.replace("'", "''")}'"


def _publish_search(*, meta_aware: bool, signature: str, comment: str, replacing: str) -> None:
    """Drop the `api.search` overload `replacing` names and publish the shape `meta_aware` picks, re-granting by exact signature."""
    op.execute(f"DROP FUNCTION {replacing}")
    op.execute(_search_body(meta_aware=meta_aware))
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_READER_ROLE}")
    op.execute(_comment(signature, comment))


def upgrade() -> None:
    """Republish `api.search` with its `include_meta` argument."""
    _publish_search(meta_aware=True, signature=_SEARCH_SIGNATURE, comment=_COMMENT_TEXT, replacing=_PREVIOUS_SEARCH_SIGNATURE)


def downgrade() -> None:
    """Put 0057's ten-argument `api.search` and its comment back."""
    _publish_search(meta_aware=False, signature=_PREVIOUS_SEARCH_SIGNATURE, comment=_PREVIOUS_COMMENT_TEXT, replacing=_SEARCH_SIGNATURE)
