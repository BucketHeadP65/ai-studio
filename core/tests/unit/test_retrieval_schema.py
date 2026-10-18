from __future__ import annotations

import dataclasses

import pytest

from core.ai.retrieval.contracts import Facet, FacetPool, GraphSchema, SearchSchema, SearchSurface
from core.ai.retrieval.schema import PATH_RANK_COLUMN, PATH_SCORE_COLUMN


def _doc_surface() -> SearchSurface:
    return SearchSurface(
        name="doc",
        table="kb.docs",
        alias="d",
        key_col="id",
        text_col="body",
        embedding_col="embedding",
        hit_level="doc",
        parent_id_expr="d.id",
        title_expr="d.title",
        bm25_index="docs_bm25",
    )


def _section_surface() -> SearchSurface:
    return SearchSurface(
        name="section",
        table="schema.sections",
        alias="s",
        key_col="id",
        text_col="body",
        embedding_col="embedding",
        hit_level="section",
        parent_id_expr="s.doc_id",
        title_expr="dp.title",
        bm25_index="sections_bm25",
        join="JOIN doc_pool dp ON dp.id = s.doc_id",
        filters=("(NOT only_current OR s.status = 'active')",),
    )


def _doc_pool() -> FacetPool:
    return FacetPool(
        name="doc_pool",
        table="kb.docs",
        alias="d",
        select_cols=("d.id", "d.title", "d.embedding"),
        predicates=(
            "(collection IS NULL OR d.collection = collection)",
            "(topic IS NULL OR topic = ANY(d.topics))",
        ),
    )


def _search_schema() -> SearchSchema:
    return SearchSchema(
        surfaces=(_doc_surface(), _section_surface()),
        pools=(_doc_pool(),),
        facets=(
            Facet(name="collection", sql_type="text", default="'general'"),
            Facet(name="topic", sql_type="text"),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        embedding_sql_type="halfvec(768)",
    )


def _graph() -> GraphSchema:
    return GraphSchema(
        edge_table="kb.edges",
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        node_view="api.nodes",
        node_cols=("id", "type", "title", "status", "superseded"),
        edge_payload_cols=("provenance",),
    )


# -- SearchSurface -----------------------------------------------------------


def test_search_surface_defaults_are_empty() -> None:
    surface = _doc_surface()
    assert surface.join == ""
    assert surface.membership == ""
    assert surface.filters == ()


@pytest.mark.parametrize("blank", ["", "   "])
def test_search_surface_rejects_blank_identifier(blank: str) -> None:
    with pytest.raises(ValueError, match=r"SearchSurface\.table"):
        SearchSurface(
            name="doc",
            table=blank,
            alias="d",
            key_col="id",
            text_col="body",
            embedding_col="embedding",
            hit_level="doc",
            parent_id_expr="d.id",
            title_expr="d.title",
            bm25_index="docs_bm25",
        )


def test_search_surface_rejects_single_quote_in_hit_level() -> None:
    # hit_level is interpolated into a single-quoted SQL string literal by the builder, so a stray
    # single quote must be rejected at construction rather than break out of that literal.
    with pytest.raises(ValueError, match="hit_level"):
        dataclasses.replace(_doc_surface(), hit_level="do'c")


# -- FacetPool / Facet -------------------------------------------------------


def test_facet_pool_requires_a_projected_column() -> None:
    with pytest.raises(ValueError, match="select_cols"):
        FacetPool(name="doc_pool", table="kb.docs", alias="d", select_cols=())


def test_facet_default_is_optional() -> None:
    assert Facet(name="topic", sql_type="text").default is None
    assert Facet(name="collection", sql_type="text", default="'general'").default == "'general'"


def test_facet_rejects_blank_type() -> None:
    with pytest.raises(ValueError, match=r"Facet\.sql_type"):
        Facet(name="topic", sql_type="")


# -- SearchSchema ------------------------------------------------------------


def test_search_schema_builds_and_exposes_surfaces() -> None:
    schema = _search_schema()
    assert len(schema.surfaces) == 2
    assert schema.embedding_sql_type == "halfvec(768)"
    assert schema.surface("section").parent_id_expr == "s.doc_id"


def test_search_schema_surface_lookup_raises_for_unknown() -> None:
    with pytest.raises(KeyError, match="missing"):
        _search_schema().surface("missing")


def test_search_schema_requires_at_least_one_surface() -> None:
    with pytest.raises(ValueError, match="at least one surface"):
        SearchSchema(surfaces=())


def test_search_schema_rejects_duplicate_surface_name() -> None:
    with pytest.raises(ValueError, match="surface names"):
        SearchSchema(surfaces=(_doc_surface(), _doc_surface()))


def test_search_schema_rejects_duplicate_hit_level() -> None:
    clashing = dataclasses.replace(_section_surface(), name="other", bm25_index="other_bm25", hit_level="doc")
    with pytest.raises(ValueError, match="hit levels"):
        SearchSchema(surfaces=(_doc_surface(), clashing))


def test_search_schema_rejects_duplicate_bm25_index() -> None:
    clashing = dataclasses.replace(_section_surface(), name="other", hit_level="other", bm25_index="docs_bm25")
    with pytest.raises(ValueError, match="BM25 index"):
        SearchSchema(surfaces=(_doc_surface(), clashing))


def test_search_schema_rejects_duplicate_pool_name() -> None:
    with pytest.raises(ValueError, match="pool names"):
        SearchSchema(surfaces=(_doc_surface(),), pools=(_doc_pool(), _doc_pool()))


def test_search_schema_rejects_duplicate_facet_name() -> None:
    with pytest.raises(ValueError, match="facet names"):
        SearchSchema(
            surfaces=(_doc_surface(),),
            facets=(Facet(name="topic", sql_type="text"), Facet(name="topic", sql_type="text")),
        )


def test_search_schema_rejects_blank_embedding_type() -> None:
    with pytest.raises(ValueError, match="embedding_sql_type"):
        SearchSchema(surfaces=(_doc_surface(),), embedding_sql_type="")


def test_search_schema_rejects_level_facet_naming_no_facet() -> None:
    with pytest.raises(ValueError, match="level_facet"):
        SearchSchema(surfaces=(_doc_surface(),), facets=(Facet(name="topic", sql_type="text"),), level_facet="level")


def test_search_schema_accepts_level_facet_that_names_a_declared_facet() -> None:
    schema = SearchSchema(surfaces=(_doc_surface(),), facets=(Facet(name="level", sql_type="text"),), level_facet="level")
    assert schema.level_facet == "level"


def test_search_schema_rejects_collection_facet_naming_no_facet() -> None:
    with pytest.raises(ValueError, match="collection_facet"):
        SearchSchema(surfaces=(_doc_surface(),), facets=(Facet(name="topic", sql_type="text"),), collection_facet="collection")


def test_search_schema_accepts_collection_facet_that_names_a_declared_facet() -> None:
    schema = SearchSchema(surfaces=(_doc_surface(),), facets=(Facet(name="collection", sql_type="text"),), collection_facet="collection")
    assert schema.collection_facet == "collection"


def test_search_schema_defaults_collection_facet_to_empty() -> None:
    """A schema with no session-scoped collection facet declares nothing; every facet keeps its own default."""
    assert SearchSchema(surfaces=(_doc_surface(),)).collection_facet == ""


# -- GraphSchema -------------------------------------------------------------


def test_graph_schema_expand_columns_are_nodes_then_edge() -> None:
    assert _graph().expand_columns == ("id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel", "provenance")


def test_graph_schema_paths_columns_append_rank_and_score() -> None:
    graph = _graph()
    assert graph.paths_columns == (*graph.expand_columns, PATH_RANK_COLUMN, PATH_SCORE_COLUMN)
    assert graph.paths_columns[-2:] == ("path_rank", "score")


def test_graph_schema_expand_columns_omit_empty_payload() -> None:
    graph = dataclasses.replace(_graph(), edge_payload_cols=())
    assert graph.expand_columns == ("id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel")


def test_graph_schema_requires_node_columns() -> None:
    with pytest.raises(ValueError, match="node_cols"):
        GraphSchema(edge_table="kb.edges", src_col="src_id", dst_col="dst_id", rel_col="rel", node_view="api.nodes", node_cols=())


def test_graph_schema_rejects_blank_edge_table() -> None:
    with pytest.raises(ValueError, match="edge_table"):
        GraphSchema(edge_table="", src_col="src_id", dst_col="dst_id", rel_col="rel", node_view="api.nodes", node_cols=("id",))


def test_graph_schema_node_col_types_defaults_to_empty() -> None:
    assert _graph().node_col_types == ()


def test_graph_schema_accepts_node_col_types_matching_node_cols() -> None:
    graph = dataclasses.replace(_graph(), node_col_types=("text", "text", "text", "text", "boolean"))
    assert graph.node_col_types == ("text", "text", "text", "text", "boolean")


def test_graph_schema_rejects_node_col_types_of_wrong_length() -> None:
    with pytest.raises(ValueError, match="node_col_types"):
        dataclasses.replace(_graph(), node_col_types=("text", "boolean"))


def test_graph_schema_partition_col_defaults_to_unset() -> None:
    assert _graph().partition_col is None


def test_graph_schema_accepts_a_partition_col() -> None:
    graph = dataclasses.replace(_graph(), partition_col="src_collection")
    assert graph.partition_col == "src_collection"


def test_graph_schema_rejects_blank_partition_col() -> None:
    with pytest.raises(ValueError, match="partition_col"):
        dataclasses.replace(_graph(), partition_col="   ")
