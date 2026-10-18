"""`api.search` compares a query vector only with the vectors of the model that made it.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-10 00:00:00.000000

A query vector ranks correctly only against vectors from the same embedding model, yet the semantic leg compared it
with every stored vector. A knowledge base indexed under another model, such as one not indexed again since the
deployment's embedding model changed, got a semantic rank that meant nothing.

`api.search` gains a trailing `query_model text DEFAULT NULL`, the model that embedded `query_vec`. Set, the semantic
leg of every tier leaves out a collection whose `kb.index_fingerprint` records a different `embed_model`. A collection
with no fingerprint row, or one that records no model, keeps its semantic leg, because nothing says its vectors differ.
Left NULL, the function ranks exactly as 0051's did. The keyword legs never change.

The nine-argument function is dropped before the ten-argument one is created, so no overload is left behind. Every
caller names its arguments or passes the first three by position, so each call keeps resolving. EXECUTE is granted again
by exact argument list, and the comment `describe_schema` serves names the new argument. Downgrade restores 0051's
function, grant and 0053's comment. What this revision freezes is the descriptor, never the rendered SQL.
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

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text)"
_PREVIOUS_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date)"


def _search_schema(*, model_aware: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `model_aware=False` is 0051's shape, byte for byte. `model_aware=True` adds the `query_model` facet, a pool of the
    collections indexed under another model, and a semantic-leg filter on every tier that leaves those out.

    Args:
        model_aware: Whether the semantic leg reads only the query's model.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """

    def outside(alias: str) -> tuple[str, ...]:
        return (f"{alias}.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",) if model_aware else ()

    model_pool = (
        (
            FacetPool(
                name="foreign_models",
                table="kb.index_fingerprint",
                alias="f",
                select_cols=("f.collection",),
                predicates=("{query_model} IS NOT NULL", "f.embed_model <> ''", "f.embed_model <> {query_model}"),
            ),
        )
        if model_aware
        else ()
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
            *((Facet(name="query_model", sql_type="text"),) if model_aware else ()),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
        partition_result_col="src_collection",
    )


def _search_body(*, model_aware: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        model_aware: Whether the semantic leg reads only the query's model.

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
    return builder.api_search_body(_search_schema(model_aware=model_aware))


# 0053's comment, byte-identical: what `downgrade` restores, and what `upgrade` extends.
_PREVIOUS_COMMENT_TEXT = (
    "Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base doors "
    "(run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into query_vec for "
    "you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller outside the doors "
    "passes query_vec, a halfvec query embedding, or NULL.) k caps how many hits come back (default 12, max 50). The "
    "'level' arg filters to one tier (card|section|claim|raw). 'facets' is one jsonb bag of facet name to value-list "
    "narrowing (e.g. '{\"<facet>\": [\"<value>\"]}'), matched against each page's own declared facets (values for one "
    "key OR'd, several keys AND'd), applied BEFORE ranking. 'collection' routes the search to one knowledge base, "
    "defaulting to the reader session's own collection (the app.collection setting) rather than any fixed one. Pass "
    "collection => NULL explicitly to rank over every collection visible to the session. 'only_current' defaults to "
    "true, which ranks only over live pages and active claims. Pass false to rank over superseded pages, stubs and "
    "retired claims too. 'since' and 'until' are dates, both inclusive and both NULL by default: either one keeps only "
    "pages whose created date falls in the range, applied BEFORE ranking, so a page with no created date and every raw "
    "chunk drop out once either is set. Returns id, src_collection, hit_level (card|section|claim|raw), page_id, "
    "page_title, snippet, score, lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the "
    "knowledge base the hit belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on "
    "src_collection and id together. snippet is an excerpt for judging whether to fetch, cut at 480 characters with a "
    "trailing ellipsis when the matched text is longer, and snippet_truncated says whether it was cut. Fetch the id for "
    "the whole unit. lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic "
    "leg's raw distance. A NULL rank means that leg did not select this row, so the pair says which components support "
    "a hit rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, since => '2026-09-01');"
)

_COMMENT_TEXT = _PREVIOUS_COMMENT_TEXT.replace(
    "(Only a direct caller outside the doors passes query_vec, a halfvec query embedding, or NULL.)",
    "(Only a direct caller outside the doors passes query_vec, a halfvec query embedding, or NULL, and query_model, "
    "the embedding model that made it. With query_model set, a knowledge base indexed under another embedding model "
    "gets no semantic leg, because its vectors are not comparable.)",
)


def _comment(signature: str, body: str) -> str:
    """The `COMMENT ON FUNCTION` statement for `signature`, with `body` quoted."""
    return f"COMMENT ON FUNCTION {signature} IS '{body.replace("'", "''")}'"


def _publish_search(*, model_aware: bool, signature: str, comment: str, replacing: str) -> None:
    """Drop the `api.search` overload `replacing` names and publish the shape `model_aware` picks, re-granting by exact signature."""
    op.execute(f"DROP FUNCTION {replacing}")
    op.execute(_search_body(model_aware=model_aware))
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_READER_ROLE}")
    op.execute(_comment(signature, comment))


def upgrade() -> None:
    """Republish `api.search` with its `query_model` argument."""
    _publish_search(model_aware=True, signature=_SEARCH_SIGNATURE, comment=_COMMENT_TEXT, replacing=_PREVIOUS_SEARCH_SIGNATURE)


def downgrade() -> None:
    """Put 0051's nine-argument `api.search` and 0053's comment back."""
    _publish_search(model_aware=False, signature=_PREVIOUS_SEARCH_SIGNATURE, comment=_PREVIOUS_COMMENT_TEXT, replacing=_SEARCH_SIGNATURE)
