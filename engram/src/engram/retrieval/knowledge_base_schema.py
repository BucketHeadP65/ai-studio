"""The knowledge base tables as a `SearchSchema`, the data the hybrid-search builder generates `api.search` from.

The four surfaces are the retrieval tiers (card, section, claim, raw). Each names its table, its text
and embedding columns, and how it reaches its parent page's title. An id is unique only within its
collection, so every tier joins its pool on the collection and the id together, and every hit returns
its collection as `src_collection`.

The two pools are the facet pre-filters the surfaces share. `page_pool` is the facet-filtered,
currency-filtered, date-filtered set of visible pages every page-derived surface scopes to, each with the
source it was made from. `raw_keys` is the visible source mirrors in the collection. A raw chunk carries
neither facets nor a page date, so once `facets`, `since` or `until` is set `raw_keys` keeps only the
sources some page in `page_pool` was made from.

The eight facets are `api.search`'s narrowing parameters. `level` gates a query to one tier by matching a
surface's `hit_level`. `facets` is one jsonb object of key to value-list narrowing (e.g.
`{"<facet>": ["<value>"]}`) matched against each page's own `facets` column, values for one key
OR'd and several keys AND'd. `collection` and `only_current` scope by collection and currency. `since`
and `until` keep pages whose page date (`kb.pages.page_date`) falls in that inclusive range, and a page
with no page date never matches one. `query_model` names the embedding model that made the query vector, and the third pool,
`foreign_models`, holds the collections indexed under another model, which every tier's semantic leg leaves out.
`include_meta` false leaves the knowledge base's own `meta.*` pages out of the page pool, before `k` is applied.

Every leg stops at a budget and the fusion weighs each tier's leg. Those numbers live in `kb.retrieval_tuning`, which the
function reads as its owner and clamps to bounds it carries. Each tier keeps a row whose key is in its pool through one
hashed lookup, never a join, so a keyword leg reads its BM25 index in score order, and reads its title from the pool.
After the fusion a card whose title is the query goes first when no other candidate holds that title, and each page or
source keeps at most `rows_per_group` hits.
"""

from __future__ import annotations

from core.ai.retrieval.contracts import Facet, FacetPool, SearchSchema, SearchSurface, SearchTuning


def _in_pages(alias: str, page_col: str) -> str:
    """Whether a row's page is in the page pool, as one hashed lookup the planner never turns into a join."""
    return f"(({alias}.collection, {alias}.{page_col}) IN (SELECT page_pool.collection, page_pool.id FROM page_pool)) IS TRUE"  # noqa: S608 (built from this module's own names)


def _page_title(alias: str, page_col: str) -> str:
    """A row's page title, read from the page pool by key."""
    return f"(SELECT page_pool.title FROM page_pool WHERE page_pool.collection = {alias}.collection AND page_pool.id = {alias}.{page_col})"  # noqa: S608 (built from this module's own names)


# The surfaces are ordered card, section, claim, raw, and their legs union in this order. The facet
# fragments reference a facet through a `{facet}` placeholder the builder renders per dialect. Both
# dialects must produce the same row set per leg.
KB_SEARCH_SCHEMA = SearchSchema(
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
            vector_filters=("p.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",),
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
            vector_filters=("s.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",),
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
            vector_filters=("c.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",),
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
            title_expr="(SELECT raw_keys.title FROM raw_keys WHERE raw_keys.collection = r.collection AND raw_keys.source_id = r.source_id)",
            bm25_index="raw_chunks_bm25",
            membership="((r.collection, r.source_id) IN (SELECT raw_keys.collection, raw_keys.source_id FROM raw_keys)) IS TRUE",
            group_expr="'source:' || r.source_id",
            partition_col="collection",
            vector_filters=("r.collection NOT IN (SELECT foreign_models.collection FROM foreign_models)",),
        ),
    ),
    pools=(
        FacetPool(
            name="page_pool",
            table="kb.pages",
            alias="p",
            select_cols=("p.collection", "p.id", "p.title", "p.card_text", "p.embedding", "p.source_id"),
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
                (
                    "(({facets} = '{}'::jsonb AND {since} IS NULL AND {until} IS NULL)"
                    " OR ((sr.collection, sr.source_id) IN (SELECT page_pool.collection, page_pool.source_id FROM page_pool)) IS TRUE)"
                ),
            ),
        ),
        FacetPool(
            name="foreign_models",
            table="kb.index_fingerprint",
            alias="f",
            select_cols=("f.collection",),
            predicates=("{query_model} IS NOT NULL", "f.embed_model <> ''", "f.embed_model <> {query_model}"),
        ),
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
