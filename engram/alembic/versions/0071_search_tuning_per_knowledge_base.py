"""`api.search` gives each folded unit a snippet, and each knowledge base sets its own cap and folded snippet length.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-19 00:00:00.000000

0070's folded units carried an id and a hit level and no text, so an agent shown a document's best hit could not judge
the document's other matching units without fetching each one. Each folded unit now carries a snippet: its own snippet
cut at a whole word to `folded_snippet_chars`, 160 by default and bound to 20 to 2000, with an ellipsis when cut, so it
is never longer than a hit's snippet.

`kb.retrieval_tuning` held one row, `id = 1`, that every knowledge base in the database shared. It now holds a default
row, whose `collection` is null, and at most one row per knowledge base, keyed by its collection. A knowledge base's
row may set only `max_hits_per_document` and `folded_snippet_chars`, and what it sets wins over the default row for
that knowledge base's hits. Every other number comes from the default row. The existing row becomes the default row,
gaining `folded_snippet_chars` at 160 unless it already names one.

`api.search` reads the cap and the folded snippet length of each hit's own knowledge base, so a search over several
knowledge bases caps each document by its own. Its arguments do not change and its columns do not either, so the
function is dropped and created again under the same signature, EXECUTE is granted again and the comment is set again.
`api.search_hits_per_document(collection)` answers one knowledge base's cap, the reader session's own when no
collection is passed, and replaces the function that took no argument.

Downgrade restores 0070's search, its cap function and both comments, deletes every knowledge base's row, gives the
default row `id = 1` back without `folded_snippet_chars`, and restores 0070's checks and table comment. What this
revision freezes is the descriptor, never the rendered SQL.
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

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"

_CAP_FUNCTION = "api.search_hits_per_document"

_CAP_SIGNATURE = f"{_CAP_FUNCTION}(text)"

_PREVIOUS_CAP_SIGNATURE = f"{_CAP_FUNCTION}()"

_CAP_COMMENT = (
    "The most hits one document of a knowledge base keeps in api.search, as the owner's max_hits_per_document in "
    "kb.retrieval_tuning sets it for that knowledge base, else for the whole database, 1 unless the owner sets "
    "another. 0 means api.search keeps every hit of that knowledge base and folds nothing. collection names the "
    "knowledge base and defaults to the reader session's own. A document is the source a unit was made from, or its "
    "own page when it was made from none."
)

# 0070's cap comment, byte-identical: what `downgrade` restores.
_PREVIOUS_CAP_COMMENT = (
    "The most hits one document keeps in api.search, as the owner's max_hits_per_document in kb.retrieval_tuning sets "
    "it, 1 unless the owner sets another. 0 means api.search keeps every hit and folds nothing. A document is the "
    "source a unit was made from, or its own page when it was made from none."
)

_NUMBERS = ("budget", "rrf_constant", "k_ceiling", "snippet_chars", "rows_per_group")

# The keys a knowledge base's own row may set. Every other key belongs to the default row alone.
_PER_KNOWLEDGE_BASE = ("max_hits_per_document", "folded_snippet_chars")


def _numbers_check(*, folded: bool) -> str:
    """The tuning rows' check that each named value is a number when present, and the cap a number or null."""
    numbers = (*_NUMBERS, "folded_snippet_chars") if folded else _NUMBERS
    clauses = [f"(NOT tuning ? '{name}' OR jsonb_typeof(tuning -> '{name}') = 'number')" for name in numbers]
    clauses.append("(NOT tuning ? 'max_hits_per_document' OR jsonb_typeof(tuning -> 'max_hits_per_document') IN ('number', 'null'))")
    return f"ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_numbers CHECK ({' AND '.join(clauses)})"


_PER_KNOWLEDGE_BASE_CHECK = (
    "ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_per_knowledge_base "
    f"CHECK (collection IS NULL OR (tuning - ARRAY[{', '.join(f"'{key}'" for key in _PER_KNOWLEDGE_BASE)}]) = '{{}}'::jsonb)"
)

_TUNING_COMMENT = (
    "COMMENT ON TABLE kb.retrieval_tuning IS "
    "'The numbers api.search ranks with. The default row, whose collection is null, holds budget (rows per leg), "
    "rrf_constant, k_ceiling, snippet_chars, rows_per_group (hits one page or source keeps), max_hits_per_document (hits "
    "one document keeps, 0 or null keeping every hit), folded_snippet_chars (the longest snippet a folded unit carries) "
    "and weights keyed <tier>:<lexical|semantic>. A knowledge base has at most one row of its own, keyed by its "
    "collection, which may set only max_hits_per_document and folded_snippet_chars and wins over the default row for "
    "that knowledge base''s hits. A key overrides the default the function carries, and the function clamps every value "
    "to its own bounds. A weights object replaces the default weights whole. The function reads this table as its "
    "owner. No reader is granted it.'"
)

# 0070's table comment, byte-identical: what `downgrade` restores.
_PREVIOUS_TUNING_COMMENT = (
    "COMMENT ON TABLE kb.retrieval_tuning IS "
    "'The numbers api.search ranks with: budget (rows per leg), rrf_constant, k_ceiling, snippet_chars, rows_per_group "
    "(hits one page or source keeps), max_hits_per_document (hits one document keeps, 0 or null keeping every hit) and "
    "weights keyed <tier>:<lexical|semantic>. A key overrides the default the function carries, and the function clamps "
    "every value to its own bounds. A weights object replaces the default weights whole. The function reads this row as "
    "its owner. No reader is granted it.'"
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


def _search_schema(*, folded_snippets: bool) -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    `folded_snippets=False` is 0070's published shape, byte for byte. `folded_snippets=True` gives each folded unit a
    snippet and reads the cap and the folded snippet length of each hit's own knowledge base.

    Args:
        folded_snippets: Whether folded units carry a snippet and the tuning is read per knowledge base.

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
    page_cols = ("p.collection", "p.id", "p.title", "p.card_text", "p.embedding", "p.source_id")
    tuning = (
        SearchTuning(relation="kb.retrieval_tuning", max_hits_per_document=1, folded_snippet_chars=160, partition_col="collection")
        if folded_snippets
        else SearchTuning(relation="kb.retrieval_tuning", max_hits_per_document=1)
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
                membership=_in_pages("p", "id"),
                title_pin=True,
                document_expr="coalesce('source:' || p.source_id, 'page:' || p.id)",
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
                document_expr=_page_document("s", "page_id"),
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
                document_expr=_page_document("c", "page_id"),
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
                document_expr="'source:' || r.source_id",
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
        tuning=tuning,
    )


def _search_body(*, folded_snippets: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

    Args:
        folded_snippets: Whether folded units carry a snippet and the tuning is read per knowledge base.

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
    return builder.api_search_body(_search_schema(folded_snippets=folded_snippets))


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
    "document keeps its best hit, so k counts documents: max_hits_per_document, which the owner sets for each "
    "knowledge base or for the whole database (1 unless the owner sets another), is how many hits one document of "
    "that knowledge base keeps, and 0 keeps every hit. A pinned card is its document's best hit. A unit's document "
    "is the source it was made from when it has one, so a page made from a source, its sections, its claims and the "
    "source's raw chunks are one document, and otherwise its own page. folded_units lists the next matching units of "
    "a document under its best hit, best first and at most 3, as a jsonb array of {id, hit_level, snippet}, and is "
    "an empty array on every other hit. A folded unit's snippet is its matched text cut at a whole word to the "
    "owner's folded snippet length for that knowledge base (160 characters unless the owner sets another, never "
    "longer than a hit's snippet) with a trailing ellipsis when the text is longer. Fetch a folded unit's id to read "
    "the whole unit. lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the "
    "semantic leg's raw distance. A NULL rank means that leg did not select this row, so the pair says which "
    "components support a hit rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, "
    "since => '2026-09-01');"
)

# 0070's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_COMMENT_TEXT = (
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


def _publish_search(*, folded_snippets: bool, comment: str) -> None:
    """Drop `api.search` and publish the shape `folded_snippets` picks under the same signature, re-granting and commenting it."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(folded_snippets=folded_snippets))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {_SEARCH_SIGNATURE} IS '{comment.replace("'", "''")}'")


def _publish_cap(*, folded_snippets: bool, signature: str, comment: str) -> None:
    """Create the cap function the shape `folded_snippets` picks, grant it to the reader and comment it."""
    op.execute(hits_per_document_body(_search_schema(folded_snippets=folded_snippets), function_name=_CAP_FUNCTION))
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_READER_ROLE}")
    op.execute(f"COMMENT ON FUNCTION {signature} IS '{comment.replace("'", "''")}'")


def upgrade() -> None:
    """Key the tuning rows by knowledge base, the existing row as the default, and publish the search and cap reading them."""
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_one_row")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_pkey")
    op.execute("ALTER TABLE kb.retrieval_tuning ADD COLUMN collection text")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP COLUMN id")
    op.execute("ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_collection UNIQUE NULLS NOT DISTINCT (collection)")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_numbers")
    op.execute(_numbers_check(folded=True))
    op.execute(_PER_KNOWLEDGE_BASE_CHECK)
    op.execute("UPDATE kb.retrieval_tuning SET tuning = jsonb_build_object('folded_snippet_chars', 160) || tuning WHERE collection IS NULL")
    op.execute(_TUNING_COMMENT)
    op.execute(f"DROP FUNCTION {_PREVIOUS_CAP_SIGNATURE}")
    # A SQL function's body is checked against the table when it is created, so the cap function follows the table.
    _publish_search(folded_snippets=True, comment=_COMMENT_TEXT)
    _publish_cap(folded_snippets=True, signature=_CAP_SIGNATURE, comment=_CAP_COMMENT)


def downgrade() -> None:
    """Put 0070's search, cap function and comments back, and the one row with `id = 1` holding the default row's numbers."""
    op.execute(f"DROP FUNCTION {_CAP_SIGNATURE}")
    op.execute("DELETE FROM kb.retrieval_tuning WHERE collection IS NOT NULL")
    op.execute("UPDATE kb.retrieval_tuning SET tuning = tuning - 'folded_snippet_chars'")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_per_knowledge_base")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_numbers")
    op.execute(_numbers_check(folded=False))
    op.execute("ALTER TABLE kb.retrieval_tuning DROP CONSTRAINT retrieval_tuning_collection")
    op.execute("ALTER TABLE kb.retrieval_tuning ADD COLUMN id integer DEFAULT 1 NOT NULL")
    op.execute("ALTER TABLE kb.retrieval_tuning DROP COLUMN collection")
    op.execute("ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE kb.retrieval_tuning ADD CONSTRAINT retrieval_tuning_one_row CHECK (id = 1)")
    op.execute(_PREVIOUS_TUNING_COMMENT)
    # A SQL function's body is checked against the table when it is created, so the cap function follows the table.
    _publish_search(folded_snippets=False, comment=_PREVIOUS_COMMENT_TEXT)
    _publish_cap(folded_snippets=False, signature=_PREVIOUS_CAP_SIGNATURE, comment=_PREVIOUS_CAP_COMMENT)
