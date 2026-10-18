"""`api.search` bounds every leg and reads its numbers from one tuning row the reader cannot write.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-10 00:00:00.000000

Every leg of 0058's search ranked every row it matched, so each hybrid search computed a distance and a rank for every
embedded row of every tier and handed all of them to the fusion. Its numbers, the fusion constant, the `k` ceiling and the
snippet length, were constants in code, and no tier or leg had a weight.

Each leg now stops at a budget of rows: a keyword leg at its best-scored rows, a semantic leg at its nearest rows, ranked
by their exact distance. The fusion weighs each tier's leg and adds its weight over the tuned constant. Every number is
read when the function runs from `kb.retrieval_tuning`, one row the function reads as its owner and no reader may read or
write, over defaults generated into the function, and clamped by bounds generated beside them: a budget of 1 to 200 and
never below `k`, a fusion constant of 1 to 1000, a `k` ceiling of at most 50, a snippet of 80 to 2000 characters and a
weight of 0 to 10. The row starts with the defaults, and its checks refuse a value that is not a number.

The arguments and the result do not change, so the function is dropped and created again under the same signature,
EXECUTE is granted again and 0058's comment is set again. Downgrade restores 0058's function and drops the row. What this
revision freezes is the descriptor, never the rendered SQL.
"""

from __future__ import annotations

import json

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

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"

# The numbers the tuning row starts with, the same defaults the function carries.
_DEFAULTS = {"budget": 50, "rrf_constant": 60.0, "k_ceiling": 50, "snippet_chars": 480, "weights": {}}

_NUMBERS = ("budget", "rrf_constant", "k_ceiling", "snippet_chars")

_TUNING_TABLE = f"""
CREATE TABLE kb.retrieval_tuning (
    id integer DEFAULT 1 NOT NULL,
    tuning jsonb DEFAULT '{{}}'::jsonb NOT NULL,
    CONSTRAINT retrieval_tuning_pkey PRIMARY KEY (id),
    CONSTRAINT retrieval_tuning_one_row CHECK (id = 1),
    CONSTRAINT retrieval_tuning_object CHECK (jsonb_typeof(tuning) = 'object'),
    CONSTRAINT retrieval_tuning_numbers CHECK ({" AND ".join(f"(NOT tuning ? '{name}' OR jsonb_typeof(tuning -> '{name}') = 'number')" for name in _NUMBERS)}),
    CONSTRAINT retrieval_tuning_weights CHECK (
        NOT tuning ? 'weights'
        OR (jsonb_typeof(tuning -> 'weights') = 'object'
            AND NOT jsonb_path_exists(tuning, '$.weights.* ? (@.type() != "number")'))
    )
)
"""

_TUNING_COMMENT = (
    "COMMENT ON TABLE kb.retrieval_tuning IS "
    "'The numbers api.search ranks with: budget (rows per leg), rrf_constant, k_ceiling, snippet_chars and weights "
    "keyed <tier>:<lexical|semantic>. A key overrides the default the function carries, and the function clamps every "
    "value to its own bounds. A weights object replaces the default weights whole. The function reads this row as its "
    "owner. No reader is granted it.'"
)


def _search_schema(*, tuned: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `tuned=False` is 0058's shape, byte for byte. `tuned=True` reads its numbers from `kb.retrieval_tuning`.

    Args:
        tuned: Whether the function reads its numbers from the tuning row.

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
            Facet(name="include_meta", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
        partition_result_col="src_collection",
        tuning=SearchTuning(relation="kb.retrieval_tuning") if tuned else None,
    )


def _search_body(*, tuned: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        tuned: Whether the function reads its numbers from the tuning row.

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
    return builder.api_search_body(_search_schema(tuned=tuned))


# 0058's comment, byte-identical, set again after each drop.
_COMMENT_TEXT = (
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
    "every raw chunk drop out once either is set. 'include_meta' defaults to true. Pass false to leave out the "
    "knowledge base's own meta.* pages (orientation, policy and schema pages) before k is applied. Returns id, "
    'src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, lexical_rank, '
    'vector_rank, vector_distance, snippet_truncated. src_collection is the knowledge base the hit belongs to. An '
    'id is unique only within its knowledge base, so join a hit to api.cards on src_collection and id together. '
    'snippet is an excerpt for judging whether to fetch, cut at 480 characters with a trailing ellipsis when the '
    'matched text is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole unit. '
    "lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic leg's raw "
    'distance. A NULL rank means that leg did not select this row, so the pair says which components support a hit '
    "rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, since => '2026-09-01');"
)


def _publish_search(*, tuned: bool) -> None:
    """Drop `api.search` and publish the shape `tuned` picks under the same signature, re-granting and commenting it."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(tuned=tuned))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {_SEARCH_SIGNATURE} IS '{_COMMENT_TEXT.replace("'", "''")}'")


def upgrade() -> None:
    """Create the tuning row with the defaults, and publish `api.search` reading it."""
    op.execute(_TUNING_TABLE)
    op.execute(_TUNING_COMMENT)
    op.execute(f"INSERT INTO kb.retrieval_tuning (id, tuning) VALUES (1, '{json.dumps(_DEFAULTS)}'::jsonb)")
    _publish_search(tuned=True)


def downgrade() -> None:
    """Put 0058's `api.search` back and drop the tuning row."""
    _publish_search(tuned=False)
    op.execute("DROP TABLE kb.retrieval_tuning")
