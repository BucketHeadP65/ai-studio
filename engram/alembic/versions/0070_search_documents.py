"""`api.search` keeps the best hit of each document and lists the document's next matching units under it.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-18 00:00:00.000000

One document yields several units: a page made from a source, its sections, its claims and the source's raw chunks.
0066's search ranked units, so one document could fill most of the `k` rows. After the cap on rows per group, each
document now keeps its best hits up to `max_hits_per_document`, 1 by default and bound to 0 to 50, a pinned card
counted as its document's best, so `k` counts documents. A unit's document is the source it was made from when it has
one, and otherwise its own page. The next three matching units of a document are listed under its best hit in a new last
column, `folded_units`, a jsonb array of `{id, hit_level}` best first, and every other hit carries an empty array. A
cap of 0 or null keeps every hit, so the ranking is 0066's.

`max_hits_per_document` joins the tuning row, which gains it at 1 unless it already names one, and the row's check
refuses a value that is neither a number nor null. The arguments do not change and the result gains its last column,
so the function is dropped and created again under the same signature, EXECUTE is granted again and the comment is set
again with the cap and the folded units. Downgrade restores 0066's function and comment, the row without the key, 0061's
check and the table comment. What this revision freezes is the descriptor, never the rendered SQL.

`api.search_hits_per_document()` answers the cap the search applies, read from the tuning row as the search reads it,
0 when every hit is kept. A reader may call it, so a host states the cap in its search tool's description. Downgrade
drops it.
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
    hits_per_document_body,
)

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"

_CAP_FUNCTION = "api.search_hits_per_document"

_CAP_COMMENT = (
    "The most hits one document keeps in api.search, as the owner's max_hits_per_document in kb.retrieval_tuning sets "
    "it, 1 unless the owner sets another. 0 means api.search keeps every hit and folds nothing. A document is the "
    "source a unit was made from, or its own page when it was made from none."
)

_NUMBERS = ("budget", "rrf_constant", "k_ceiling", "snippet_chars", "rows_per_group")


def _numbers_check(*, capped: bool) -> str:
    """The tuning row's check that each named value is a number when present, and the cap a number or null."""
    clauses = [f"(NOT tuning ? '{name}' OR jsonb_typeof(tuning -> '{name}') = 'number')" for name in _NUMBERS]
    if capped:
        clauses.append("(NOT tuning ? 'max_hits_per_document' OR jsonb_typeof(tuning -> 'max_hits_per_document') IN ('number', 'null'))")
    return f"ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_numbers CHECK ({' AND '.join(clauses)})"


_TUNING_COMMENT = (
    "COMMENT ON TABLE kb.retrieval_tuning IS "
    "'The numbers api.search ranks with: budget (rows per leg), rrf_constant, k_ceiling, snippet_chars, rows_per_group "
    "(hits one page or source keeps), max_hits_per_document (hits one document keeps, 0 or null keeping every hit) and "
    "weights keyed <tier>:<lexical|semantic>. A key overrides the default the function carries, and the function clamps "
    "every value to its own bounds. A weights object replaces the default weights whole. The function reads this row as "
    "its owner. No reader is granted it.'"
)

# 0061's table comment, byte-identical: what `downgrade` restores.
_PREVIOUS_TUNING_COMMENT = (
    "COMMENT ON TABLE kb.retrieval_tuning IS "
    "'The numbers api.search ranks with: budget (rows per leg), rrf_constant, k_ceiling, snippet_chars, rows_per_group "
    "(hits one page or source keeps) and weights keyed <tier>:<lexical|semantic>. A key overrides the default the function "
    "carries, and the function clamps every value to its own bounds. A weights object replaces the default weights whole. "
    "The function reads this row as its owner. No reader is granted it.'"
)

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


def _page_document(alias: str, page_col: str) -> str:
    """The document of a row inside a page: the source the page was made from, read from the page pool, else the page."""
    source = f"(SELECT page_pool.source_id FROM page_pool WHERE page_pool.collection = {alias}.collection AND page_pool.id = {alias}.{page_col})"
    return f"coalesce('source:' || {source}, 'page:' || {alias}.{page_col})"


def _search_schema(*, documented: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `documented=False` is 0066's shape, byte for byte. `documented=True` names each surface's document and keeps at
    most `max_hits_per_document` hits of one document, folding the next ones under its best.

    Args:
        documented: Whether the search caps the hits of one document and returns the folded units.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """

    def outside(alias: str) -> tuple[str, ...]:
        return (f"{alias}.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",)

    def document(expression: str) -> str:
        return expression if documented else ""

    model_pool = (
        FacetPool(
            name="foreign_models",
            table="kb.index_fingerprint",
            alias="f",
            select_cols=("f.collection",),
            predicates=("{query_model} IS NOT NULL", "f.embed_model <> ''", "f.embed_model <> {query_model}"),
        ),
    )
    page_cols = ("p.collection", "p.id", "p.title", "p.card_text", "p.embedding", "p.source_id")
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
                document_expr=document("coalesce('source:' || p.source_id, 'page:' || p.id)"),
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
                document_expr=document(_page_document("s", "page_id")),
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
                document_expr=document(_page_document("c", "page_id")),
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
                document_expr=document("'source:' || r.source_id"),
                partition_col="collection",
                vector_filters=outside("r"),
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="kb.pages",
                alias="p",
                select_cols=page_cols,
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                     "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                     "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                    "api.visible_collection(p.collection)",
                    "({since} IS NULL OR p.page_date >= {since})",
                    "({until} IS NULL OR p.page_date <= {until})",
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
                    _SOURCED,
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
        tuning=SearchTuning(relation="kb.retrieval_tuning", max_hits_per_document=1 if documented else None),
    )


def _search_body(*, documented: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        documented: Whether the search caps the hits of one document and returns the folded units.

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
    return builder.api_search_body(_search_schema(documented=documented))


_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base '
    "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
    'query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller '
    'outside the doors passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model '
    'that made it. With query_model set, a knowledge base indexed under another embedding model gets no semantic '
    'leg, because its vectors are not comparable.) k caps how many hits come back (default 12, at most the '
    "owner's k ceiling, 50 unless the owner sets it lower). The 'level' arg filters to one tier "
    '(card|section|claim|raw). \'facets\' is one jsonb bag of facet name to value-list narrowing (e.g. \'{"<facet>": '
    '["<value>"]}\'), matched against each page\'s own declared facets (values for one key OR\'d, several keys '
    "AND'd), applied BEFORE ranking. 'collection' routes the search to one knowledge base, defaulting to the "
    "reader session's own collection (the app.collection setting) rather than any fixed one. Pass collection => "
    "NULL explicitly to rank over every collection visible to the session. 'only_current' defaults to true, which "
    'ranks only over live pages and active claims. Pass false to rank over superseded pages, stubs and retired '
    "claims too. 'since' and 'until' are dates, both inclusive and both NULL by default: either one keeps only "
    'pages whose page date falls in the range, applied BEFORE ranking, so a page with no page date drops out once '
    "either is set. A page date is the day a page is about, read from the frontmatter field the knowledge base's "
    "schema names for it, the day the page was written unless it names another. A raw chunk passes 'facets', "
    "'since' and 'until' only when a page made from its source passes them, a live one unless only_current is "
    "false. With none of the three set, every raw chunk ranks. 'include_meta' defaults to true. Pass false to "
    "leave out the knowledge base's own meta.* pages (orientation, policy and schema pages) before k is applied. "
    'Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, '
    'lexical_rank, vector_rank, vector_distance, snippet_truncated, folded_units. src_collection is the knowledge '
    'base the hit belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on '
    "src_collection and id together. snippet is an excerpt for judging whether to fetch, cut at the owner's "
    'snippet length (480 characters unless the owner sets another, between 80 and 2,000) with a trailing ellipsis '
    'when the matched text is longer, and snippet_truncated says whether it was cut. Fetch the id for the whole '
    'unit. After ranking, a card whose title equals the query, ignoring case and surrounding spaces, comes first '
    'when no other card holds that title, and each page (its card, sections and claims together) or source keeps '
    'at most its best few hits (3 unless the owner sets another), the pinned card outside that count. Then each '
    "document keeps its best hit, so k counts documents: the owner's max_hits_per_document (1 unless the owner "
    "sets another) is how many hits one document keeps, and 0 keeps every hit. A pinned card is its document's "
    "best hit. A unit's document is the source it was made from when it has one, so a page made from a source, "
    "its sections, its claims and the source's raw chunks are one document, and otherwise its own page. "
    'folded_units lists the next matching units of a document under its best hit, best first and at most 3, as a '
    "jsonb array of {id, hit_level}, and is an empty array on every other hit. Fetch a folded unit's id to read "
    "that part. lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic "
    "leg's raw distance. A NULL rank means that leg did not select this row, so the pair says which components "
    "support a hit rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, since => "
    "'2026-09-01');"
)

# 0066's comment, byte-identical: what `downgrade` restores.
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
    'dates, both inclusive and both NULL by default: either one keeps only pages whose page date falls in the '
    'range, applied BEFORE ranking, so a page with no page date drops out once either is set. A page date is the day '
    "a page is about, read from the frontmatter field the knowledge base's schema names for it, the day the page was "
    'written unless it names another. A raw chunk passes '
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


def _publish_search(*, documented: bool, comment: str) -> None:
    """Drop `api.search` and publish the shape `documented` picks under the same signature, re-granting and commenting it."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(documented=documented))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {_SEARCH_SIGNATURE} IS '{comment.replace("'", "''")}'")


def upgrade() -> None:
    """Check and seed `max_hits_per_document`, publish `api.search` keeping the best hit of each document, and its cap."""
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_numbers")
    op.execute(_numbers_check(capped=True))
    op.execute("UPDATE kb.retrieval_tuning SET tuning = jsonb_build_object('max_hits_per_document', 1) || tuning WHERE id = 1")
    op.execute(_TUNING_COMMENT)
    _publish_search(documented=True, comment=_COMMENT_TEXT)
    op.execute(hits_per_document_body(_search_schema(documented=True), function_name=_CAP_FUNCTION))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CAP_FUNCTION}() TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {_CAP_FUNCTION}() IS '{_CAP_COMMENT.replace("'", "''")}'")


def downgrade() -> None:
    """Drop the cap's function, put 0066's `api.search` and comment, the row without the cap, 0061's check and table comment back."""
    op.execute(f"DROP FUNCTION {_CAP_FUNCTION}()")
    _publish_search(documented=False, comment=_PREVIOUS_COMMENT_TEXT)
    op.execute("UPDATE kb.retrieval_tuning SET tuning = tuning - 'max_hits_per_document' WHERE id = 1")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_numbers")
    op.execute(_numbers_check(capped=False))
    op.execute(_PREVIOUS_TUNING_COMMENT)
