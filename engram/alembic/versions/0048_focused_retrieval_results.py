"""`api.search` says what found each hit, and the graph walk takes a currency argument.

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-14 00:00:00.000000

`api.search` returns four columns beyond the fused score: `lexical_rank` and `vector_rank` (each
leg's own rank, NULL where that leg did not select the row), `vector_distance` (the semantic leg's
raw distance), and `snippet_truncated` (whether the excerpt was cut). `snippet` itself is now a
bounded excerpt rather than the whole matched text. The searchable shape is unchanged from 0041, so
the function is regenerated from the same frozen descriptor through the same builders: a database
already at 0047 picks the new result columns up here, and its `COMMENT` stops under-reporting them.

`api.graph_expand` and `api.graph_paths` gain a trailing `include_superseded boolean DEFAULT false`.
False, the default, walks only current nodes: a superseded node is skipped at the hop that would
reach it, so nothing reachable only through one comes back either. True walks them too. The caller's
own seed is never judged, so asking what a superseded page connects to stays possible. The traversal
under each `api` wrapper (`kb.graph_expand_walk` / `kb.graph_paths_walk`) is regenerated from the
descriptor that declares the currency column, and the five-argument functions are dropped before the
six-argument ones are created, so no overload is left behind for a call to resolve against.

Under Apache AGE a node's currency is a vertex property, so `kb.refresh_graph()` is regenerated to
project it and run once here: without that re-projection every vertex would read NULL and the
currency filter would keep every path. The projection reads currency off `kb.pages`, not off
`api.cards`: the view serves only what the reading session may see, this runs with no session
collection of its own, and a projection is read back by every caller — so a collection that keeps
out of the shared library would have every one of its pages projected as current. Under the
recursive-CTE backend the routine stays the no-op it has always been, and the walk reads currency off
the node view inside the caller's own query, where the session is the caller's.

`corpus_reader` became `kb_reader` at 0046, both `api` wrappers stay `SECURITY DEFINER` with a
pinned `search_path` and keep the `api.visible_collection(p_collection)` guard 0037 gave them, and
EXECUTE is re-granted by exact argument list after every drop. Downgrade puts the five-argument
signatures, the pre-currency traversal, the projection without currency and 0041's `api.search`
comment back. What every revision here freezes is the descriptor, never the rendered SQL, so a
generated body always reads as the library renders it today: a downgrade restores 0047's descriptor,
signatures, grants and comments rather than a body frozen as text.
"""

from __future__ import annotations

from typing import NamedTuple

from alembic import context, op
from core.ai.retrieval.contracts import (
    AgeGraphTraversalBuilder,
    CteGraphTraversalBuilder,
    Facet,
    FacetPool,
    GraphSchema,
    ParadeDBHybridSearchBuilder,
    PgTextSearchHybridSearchBuilder,
    SearchSchema,
    SearchSurface,
)

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

# `api.search`'s argument list does not change here — only the columns it returns — so one signature
# serves the DROP, the GRANT and the COMMENT on both paths.
_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean)"

# The `api` traversal signatures this revision publishes, and the ones it replaces. Every DROP and
# GRANT names an exact argument list, so the currency argument never leaves a second overload behind.
_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer, boolean)"
_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer, boolean)"
_PREVIOUS_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer)"
_PREVIOUS_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer)"

# The generated walk each `api` wrapper calls, ungranted and reached only through the wrapper (0037),
# in the `kb` schema 0046 renamed it into.
_EXPAND_WALK = "kb.graph_expand_walk"
_PATHS_WALK = "kb.graph_paths_walk"
_EXPAND_WALK_SIGNATURE = f"{_EXPAND_WALK}(text, text[], integer, text[], integer, boolean)"
_PATHS_WALK_SIGNATURE = f"{_PATHS_WALK}(text, text, text, integer, text[], integer, boolean)"
_PREVIOUS_EXPAND_WALK_SIGNATURE = f"{_EXPAND_WALK}(text, text[], integer, text[], integer)"
_PREVIOUS_PATHS_WALK_SIGNATURE = f"{_PATHS_WALK}(text, text, text, integer, text[], integer)"

# The writer-only projection rebuild, and the Apache AGE property graph it rebuilds (0046's names).
_REFRESH_SIGNATURE = "kb.refresh_graph()"
_AGE_GRAPH_NAME = "kb_graph"

# The relation the Apache AGE projection reads a page's currency from. `api.cards` serves only the
# collections the reading session may see, and this runs with no session collection of its own, so
# reading currency from the view would write `current` onto every page of a collection that keeps
# out of the shared library. The page table answers for every collection.
_PAGE_CURRENCY_SOURCE = "(SELECT p.id, p.collection AS src_collection, p.superseded_by IS NOT NULL AS superseded FROM kb.pages p)"


def _search_schema() -> SearchSchema:
    """The hybrid-search shape `api.search` carries, frozen at this revision.

    Migration-local, the convention every revision that regenerates `api.search` follows (0037,
    0041): a snapshot of the searchable shape at this revision, so a later change to the serving
    layer's own descriptor never rewrites what this revision publishes. Byte-identical to 0041's
    shape under the `kb` table names 0046 renamed them to, including the two
    `api.visible_collection` pool predicates 0037 added, so only the builder's rendering of the body
    differs from what 0047 leaves on a database.

    Returns:
        The knowledge base schema the configured dialect's builder generates `api.search` from.
    """
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
                ),
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text"),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
    )


def _search_body() -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar.

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
    return builder.api_search_body(_search_schema())


def _graph_schema(*, edge_table: str, current: bool) -> GraphSchema:
    """The link-graph shape the walk is generated from, frozen at this revision.

    Args:
        edge_table: The relation the walk reads (`kb.edges` under Apache AGE, `kb.visible_edges`
            under the recursive-CTE backend).
        current: `True` names the node view's currency column and the unnarrowed relation the
            Apache AGE projection reads currency from, which is what gives both walks their trailing
            `include_superseded` argument and makes the projection carry a vertex's currency.
            `False` is 0037's shape, walking every node regardless of currency and taking no
            currency argument.

    Returns:
        The descriptor the configured backend's builder generates the traversal from.
    """
    return GraphSchema(
        edge_table=edge_table,
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        edge_payload_cols=("provenance_claim",),
        node_view="api.cards",
        node_cols=("id", "type", "title", "status", "superseded"),
        node_col_types=("text", "text", "text", "text", "boolean"),
        partition_col="src_collection",
        superseded_col="superseded" if current else None,
        currency_source=_PAGE_CURRENCY_SOURCE if current else None,
    )


def _graph_backend_name() -> str:
    """The configured graph backend's name (`age` or `cte`), the way 0026 and 0037 read it.

    Returns:
        The backend name.

    Raises:
        RuntimeError: When `alembic/env.py` stashed no recognised `GraphBackendSql`.
    """
    backend_name = getattr(context.config.attributes.get("graph_backend"), "name", None)
    if backend_name in ("age", "cte"):
        return backend_name
    msg = f"graph_backend not configured (or unrecognised name {backend_name!r}): alembic/env.py must stash a GraphBackendSql"
    raise RuntimeError(msg)


def _graph_builder(backend_name: str) -> AgeGraphTraversalBuilder | CteGraphTraversalBuilder:
    """The traversal builder for one backend name, over the property graph 0046 renamed to `kb_graph`."""
    return AgeGraphTraversalBuilder(_AGE_GRAPH_NAME) if backend_name == "age" else CteGraphTraversalBuilder()


# The cypher walk keeps every hop inside `p_collection`, so it needs no edge-level visibility
# filter; the recursive-CTE walk crosses collections at its destination endpoint, so it reads the
# both-endpoints-visible view instead of the edge table (0037).
_WALK_EDGE_TABLE = {"age": "kb.edges", "cte": "kb.visible_edges"}

# The two wrappers' volatility follows the walk they call: the CTE bodies are STABLE reads, the
# AGE bodies are VOLATILE (they run cypher through EXECUTE).
_WALK_VOLATILITY = {"age": "VOLATILE", "cte": "STABLE"}

_EXPAND_WRAPPER = """
CREATE FUNCTION api.graph_expand(
  p_collection text,
  seed_ids text[],
  hops integer DEFAULT 2,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 20,
  include_superseded boolean DEFAULT false
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT w.*
    FROM kb.graph_expand_walk(p_collection, seed_ids, hops, rel_types, k, include_superseded) w
    WHERE api.visible_collection(p_collection)
$$
"""

_PATHS_WRAPPER = """
CREATE FUNCTION api.graph_paths(
  p_collection text,
  from_id text,
  to_id text,
  max_hops integer DEFAULT 4,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 10,
  include_superseded boolean DEFAULT false
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text,
    path_rank integer, score double precision
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT w.*
    FROM kb.graph_paths_walk(p_collection, from_id, to_id, max_hops, rel_types, k, include_superseded) w
    WHERE api.visible_collection(p_collection)
$$
"""

# 0037's wrappers under 0046's schema name: what `downgrade` restores.
_PREVIOUS_EXPAND_WRAPPER = """
CREATE FUNCTION api.graph_expand(
  p_collection text,
  seed_ids text[],
  hops integer DEFAULT 2,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 20
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT w.*
    FROM kb.graph_expand_walk(p_collection, seed_ids, hops, rel_types, k) w
    WHERE api.visible_collection(p_collection)
$$
"""

_PREVIOUS_PATHS_WRAPPER = """
CREATE FUNCTION api.graph_paths(
  p_collection text,
  from_id text,
  to_id text,
  max_hops integer DEFAULT 4,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 10
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text,
    path_rank integer, score double precision
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT w.*
    FROM kb.graph_paths_walk(p_collection, from_id, to_id, max_hops, rel_types, k) w
    WHERE api.visible_collection(p_collection)
$$
"""

_SEARCH_COMMENT = (
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

# 0041's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_SEARCH_COMMENT = (
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

_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], int, text[], int, boolean) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in "
    "p_collection, walk the link graph out to N hops (default 2), optionally filtered to rel_types, "
    "returning one row per reached node — its id/type/title/status/superseded plus the edge that "
    "reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how many rows come "
    "back (max 50). include_superseded defaults to false, which skips a superseded node at the hop "
    "that would reach it, so a node reachable only through one is absent too; pass true to walk "
    "them. A seed is never judged, so expanding a superseded page still answers. Example: "
    "SELECT * FROM api.graph_expand(''research'', ARRAY[''ent.graphrag''], hops => 2);'"
)
_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, int, text[], int, boolean) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths "
    "from from_id to to_id within p_collection within max_hops (default 4), optionally filtered to "
    "rel_types, returning one row per node along each path — its id/type/title/status/superseded plus "
    "the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the path_rank "
    "group and the path score. k caps the number of paths. include_superseded defaults to false, "
    "which drops any path running through a superseded node past the start; pass true to keep them. "
    "Example: SELECT * FROM api.graph_paths(''research'', ''ent.graphrag'', ''ent.lightrag'');'"
)

# 0026's comments under 0046's signatures: what `downgrade` restores.
_PREVIOUS_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in "
    "p_collection, walk the link graph out to N hops (default 2), optionally filtered to rel_types, "
    "returning one row per reached node — its id/type/title/status/superseded plus the edge that "
    "reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how many rows come "
    "back (max 50). Example: SELECT * FROM api.graph_expand(''research'', ARRAY[''ent.graphrag''], hops => 2);'"
)
_PREVIOUS_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, int, text[], int) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths "
    "from from_id to to_id within p_collection within max_hops (default 4), optionally filtered to "
    "rel_types, returning one row per node along each path — its id/type/title/status/superseded plus "
    "the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the path_rank "
    "group and the path score. k caps the number of paths. Example: "
    "SELECT * FROM api.graph_paths(''research'', ''ent.graphrag'', ''ent.lightrag'');'"
)

# The comment re-applied after the drop (a drop takes a comment with it). It names `kb.edges`, the
# relation the routine reads under the name 0046 gave it, and the currency this revision puts on
# every vertex.
_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION kb.refresh_graph() IS "
    "'Rebuild the projected link graph from kb.edges, keyed by (id, collection) and carrying each "
    "vertex and edge''s own collection so two collections sharing a bare id never merge into one "
    "vertex, plus each vertex''s own currency read off kb.pages so a traversal can skip a "
    "superseded node (a no-op under the recursive-CTE backend, which walks the edge relation live "
    "and needs no projected graph). Writer-only — run by the index load step after the edge set is "
    "replaced; the read-only api.graph_expand/api.graph_paths functions only traverse the "
    "projection it produces.'"
)

# The comment a database carries at 0047, byte-identical: what `downgrade` restores.
_PREVIOUS_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION kb.refresh_graph() IS "
    "'Rebuild the projected link graph from memory.edges, keyed by (id, collection) and carrying "
    "each vertex and edge''s own collection so two collections sharing a bare id never merge into "
    "one vertex (a no-op under the recursive-CTE backend, which walks the edge relation live and "
    "needs no projected graph). Writer-only — run by the compile load step after the edge set is "
    "replaced; the read-only api.graph_expand/api.graph_paths functions only traverse the "
    "projection it produces.'"
)


class _Traversal(NamedTuple):
    """One published shape of the graph traversal: what it is called, what it reads, and what it says.

    Attributes:
        expand_signature: The exact `api.graph_expand` argument list, as DROP and GRANT name it.
        paths_signature: The exact `api.graph_paths` argument list, as DROP and GRANT name it.
        expand_walk_signature: The exact `kb.graph_expand_walk` argument list, as DROP names it.
        paths_walk_signature: The exact `kb.graph_paths_walk` argument list, as DROP names it.
        expand_wrapper: The `CREATE FUNCTION api.graph_expand` statement, with a `{volatility}` hole
            the backend fills.
        paths_wrapper: The `CREATE FUNCTION api.graph_paths` statement, with the same hole.
        expand_comment: The `COMMENT ON FUNCTION api.graph_expand` statement.
        paths_comment: The `COMMENT ON FUNCTION api.graph_paths` statement.
        refresh_comment: The `COMMENT ON FUNCTION kb.refresh_graph` statement.
        current: Whether the walk reads a node's currency, which is what gives both functions their
            trailing `include_superseded` argument and puts a vertex's currency in the Apache AGE
            projection.
    """

    expand_signature: str
    paths_signature: str
    expand_walk_signature: str
    paths_walk_signature: str
    expand_wrapper: str
    paths_wrapper: str
    expand_comment: str
    paths_comment: str
    refresh_comment: str
    current: bool


_CURRENCY_AWARE = _Traversal(
    expand_signature=_EXPAND_SIGNATURE,
    paths_signature=_PATHS_SIGNATURE,
    expand_walk_signature=_EXPAND_WALK_SIGNATURE,
    paths_walk_signature=_PATHS_WALK_SIGNATURE,
    expand_wrapper=_EXPAND_WRAPPER,
    paths_wrapper=_PATHS_WRAPPER,
    expand_comment=_EXPAND_COMMENT,
    paths_comment=_PATHS_COMMENT,
    refresh_comment=_REFRESH_COMMENT,
    current=True,
)

_CURRENCY_BLIND = _Traversal(
    expand_signature=_PREVIOUS_EXPAND_SIGNATURE,
    paths_signature=_PREVIOUS_PATHS_SIGNATURE,
    expand_walk_signature=_PREVIOUS_EXPAND_WALK_SIGNATURE,
    paths_walk_signature=_PREVIOUS_PATHS_WALK_SIGNATURE,
    expand_wrapper=_PREVIOUS_EXPAND_WRAPPER,
    paths_wrapper=_PREVIOUS_PATHS_WRAPPER,
    expand_comment=_PREVIOUS_EXPAND_COMMENT,
    paths_comment=_PREVIOUS_PATHS_COMMENT,
    refresh_comment=_PREVIOUS_REFRESH_COMMENT,
    current=False,
)


def _publish_traversal(published: _Traversal, *, replacing: _Traversal) -> None:
    """Drop the traversal `replacing` names and publish `published`, re-granting by exact signature.

    Every function is dropped by its own argument list before the replacement is created, so a call
    can never resolve against a left-behind overload, and EXECUTE is granted again afterwards
    because a drop takes the grant with it. The projection refresh is re-run at the end rather than
    left to the next index run: under Apache AGE a node's currency is a vertex property, so the
    traversal only ever sees what the last refresh projected.

    Args:
        published: The traversal shape this call leaves on the database.
        replacing: The traversal shape already on the database, dropped first.
    """
    backend_name = _graph_backend_name()
    builder = _graph_builder(backend_name)
    schema = _graph_schema(edge_table=_WALK_EDGE_TABLE[backend_name], current=published.current)
    volatility = _WALK_VOLATILITY[backend_name]

    op.execute(f"DROP FUNCTION {replacing.expand_signature}")
    op.execute(f"DROP FUNCTION {replacing.paths_signature}")
    op.execute(f"DROP FUNCTION {replacing.expand_walk_signature}")
    op.execute(f"DROP FUNCTION {replacing.paths_walk_signature}")
    op.execute(f"DROP FUNCTION {_REFRESH_SIGNATURE}")

    op.execute(builder.expand_body(schema, function_name=_EXPAND_WALK))
    op.execute(builder.paths_body(schema, function_name=_PATHS_WALK))
    op.execute(builder.refresh_body(schema))
    op.execute(published.expand_wrapper.format(volatility=volatility))
    op.execute(published.paths_wrapper.format(volatility=volatility))

    op.execute(f"GRANT EXECUTE ON FUNCTION {published.expand_signature} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {published.paths_signature} TO {_READER_ROLE}")
    op.execute(published.expand_comment)
    op.execute(published.paths_comment)
    op.execute(published.refresh_comment)
    op.execute(f"SELECT {_REFRESH_SIGNATURE}")


def upgrade() -> None:
    """Republish `api.search` with its per-leg facts, and the graph traversal with its currency argument."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body())
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_SEARCH_COMMENT)

    _publish_traversal(_CURRENCY_AWARE, replacing=_CURRENCY_BLIND)


def downgrade() -> None:
    """Put 0041's `api.search` comment and the five-argument, currency-blind traversal back."""
    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body())
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_PREVIOUS_SEARCH_COMMENT)

    _publish_traversal(_CURRENCY_BLIND, replacing=_CURRENCY_AWARE)
