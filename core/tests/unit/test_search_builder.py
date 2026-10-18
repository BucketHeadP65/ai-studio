"""Structural tests for the hybrid-search builders.

The builders emit deterministic SQL text from a `SearchSchema`, so these assert the generated body's
structure (the invariant skeleton, the per-dialect grammar and workaround shape, and that no
placeholder leaks) rather than executing it. The knowledge base fixture is a generic two-surface knowledge base, not
any consumer's, so the library stays knowledge-base-agnostic. Behavioral parity against a real database is
proven by the consumer's own integration suite.
"""

from __future__ import annotations

import re

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import (
    FOLDED_UNITS_PER_HIT,
    Facet,
    FacetPool,
    ParadeDBHybridSearchBuilder,
    PgTextSearchHybridSearchBuilder,
    SearchSchema,
    SearchSurface,
    SearchTuning,
)

if TYPE_CHECKING:
    from core.ai.retrieval.search_builder import BaseHybridSearchBuilder


def _corpus() -> SearchSchema:
    """A generic two-surface knowledge_base: a top-level `doc` (with a pool semi-join) and a nested `chunk`."""
    return SearchSchema(
        surfaces=(
            SearchSurface(
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
                membership="d.id IN (SELECT doc_pool.id FROM doc_pool)",
            ),
            SearchSurface(
                name="chunk",
                table="kb.chunks",
                alias="c",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="chunk",
                parent_id_expr="c.doc_id",
                title_expr="dp.title",
                bm25_index="chunks_bm25",
                join="JOIN doc_pool dp ON dp.id = c.doc_id",
                filters=("(NOT {only_current} OR c.active)",),
            ),
        ),
        pools=(
            FacetPool(
                name="doc_pool",
                table="kb.docs",
                alias="d",
                select_cols=("d.id", "d.title", "d.body", "d.embedding"),
                predicates=("({topic} IS NULL OR {topic} = ANY(d.topics))", "(NOT {only_current} OR d.published)"),
            ),
        ),
        facets=(Facet("level", "text"), Facet("topic", "text"), Facet("only_current", "boolean", "true")),
        embedding_sql_type="halfvec(768)",
        level_facet="level",
    )


_BUILDERS = [ParadeDBHybridSearchBuilder(), PgTextSearchHybridSearchBuilder()]


@pytest.mark.parametrize("builder", _BUILDERS)
def test_generates_the_invariant_skeleton(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert body.startswith("CREATE FUNCTION api.search(")
    assert "RETURNS TABLE (id text, hit_level text, page_id text, page_title text, snippet text, score float8, " in body
    assert "LANGUAGE plpgsql STABLE SECURITY DEFINER" in body
    assert "capped_k int := least(greatest(k, 1), 50);" in body
    assert "sum(1.0 / (60 + legs.r))::float8 AS rrf" in body
    assert "GROUP BY legs.id" in body
    assert "ORDER BY rrf DESC" in body
    assert "BEGIN" in body
    assert "END;" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_signature_declares_the_fixed_inputs_then_the_facets(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert "k int DEFAULT 12" in body
    assert "query_vec halfvec(768) DEFAULT NULL" in body
    assert "level text DEFAULT NULL" in body
    assert "topic text DEFAULT NULL" in body
    assert "only_current boolean DEFAULT true" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_search_path_is_derived_from_the_qualified_tables(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert "SET search_path = pg_catalog, kb, public, pg_temp" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_every_surface_contributes_a_keyword_and_a_vector_leg(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    for leg in ("doc_bm25", "doc_vec", "chunk_bm25", "chunk_vec"):
        assert f"{leg} AS " in body
        assert f"FROM {leg}" in body
    # The legs union is relation-qualified (required by the static body's use_variable, harmless elsewhere).
    assert (
        "SELECT doc_bm25.id, doc_bm25.lvl, doc_bm25.page_id, doc_bm25.page_title, doc_bm25.snippet, "
        "doc_bm25.snippet_truncated, doc_bm25.leg, doc_bm25.vector_distance, doc_bm25.r FROM doc_bm25"
    ) in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_placeholder_leaks_into_the_generated_body(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert "{" not in body
    assert "}" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_generation_is_deterministic(builder: BaseHybridSearchBuilder) -> None:
    schema = _corpus()
    assert builder.api_search_body(schema) == builder.api_search_body(schema)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_function_name_is_overridable(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus(), function_name="search.hybrid")
    assert body.startswith("CREATE FUNCTION search.hybrid(")


def test_paradedb_grammar_and_workaround_shape() -> None:
    body = ParadeDBHybridSearchBuilder().api_search_body(_corpus())
    # Dynamic body, parameters bound positionally in referand order (query, capped_k, query_vec, *facets).
    assert "RETURN QUERY EXECUTE $q$" in body
    assert "$q$ USING query, capped_k, query_vec, level, topic, only_current;" in body
    assert "AS MATERIALIZED (" in body
    # BM25 grammar + positional references.
    assert "d.body @@@ paradedb.parse($1)" in body
    assert "row_number() OVER (ORDER BY paradedb.score(d.id) DESC)" in body
    assert "($4 IS NULL OR $4 = 'doc')" in body  # the level filter, positional
    assert "(NOT $6 OR c.active)" in body  # the chunk per-surface filter, positional
    assert "d.embedding <=> $3" in body
    # None of the static-body machinery leaks in.
    assert "#variable_conflict" not in body
    assert "<@>" not in body


def test_pg_textsearch_grammar_and_workaround_shape() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_corpus())
    # Static body, parameters bound by name, conflicts resolved to the variable.
    assert "#variable_conflict use_variable" in body
    assert "RETURN QUERY\n" in body
    assert "RETURN QUERY EXECUTE" not in body
    assert "AS MATERIALIZED (" not in body
    # BM25 grammar: <@> ranks, filters (< 0), and takes a per-leg top-k slice.
    assert "d.body <@> to_bm25query(query, 'docs_bm25') < 0" in body
    assert "row_number() OVER (ORDER BY d.body <@> to_bm25query(query, 'docs_bm25'))" in body
    assert "LIMIT capped_k" in body  # the per-leg tail, absent on ParadeDB
    assert "(level IS NULL OR level = 'doc')" in body  # the level filter, by name
    assert "(NOT only_current OR c.active)" in body  # the chunk per-surface filter, by name
    assert "d.embedding <=> query_vec" in body
    # None of the dynamic-body machinery leaks in.
    assert "pg_search" not in body
    assert "$1" not in body


def test_membership_semi_join_is_emitted_for_the_top_level_surface() -> None:
    # The nested surface is scoped by its JOIN, so only the top-level surface carries the semi-join.
    for builder in _BUILDERS:
        body = builder.api_search_body(_corpus())
        assert "d.id IN (SELECT doc_pool.id FROM doc_pool)" in body


def _corpus_with_jsonb_facet() -> SearchSchema:
    """A one-surface knowledge base whose pool narrows on a jsonb facet.

    Isolated from `_corpus()` so the literal `{}` inside the jsonb default/predicate never trips
    the other tests' placeholder-leak assertion (that assertion only holds for a knowledge base with no
    jsonb facet).
    """
    return SearchSchema(
        surfaces=(
            SearchSurface(
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
                membership="d.id IN (SELECT doc_pool.id FROM doc_pool)",
            ),
        ),
        pools=(
            FacetPool(
                name="doc_pool",
                table="kb.docs",
                alias="d",
                select_cols=("d.id", "d.title", "d.body", "d.embedding"),
                predicates=(
                    (
                        "({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                        "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                        "  WHERE (d.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"
                    ),
                ),
            ),
        ),
        facets=(Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),),
    )


@pytest.mark.parametrize("builder", _BUILDERS)
def test_jsonb_facet_default_renders_as_the_sql_literal(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus_with_jsonb_facet())
    assert "facets jsonb DEFAULT '{}'::jsonb" in body


def test_paradedb_renders_the_jsonb_facet_predicate_with_a_positional_marker() -> None:
    body = ParadeDBHybridSearchBuilder().api_search_body(_corpus_with_jsonb_facet())
    assert "($4 = '{}'::jsonb OR NOT EXISTS (" in body
    assert "SELECT 1 FROM jsonb_each($4) AS f(key, vals)" in body
    assert "(d.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))" in body
    assert "$q$ USING query, capped_k, query_vec, facets;" in body


def test_pg_textsearch_renders_the_jsonb_facet_predicate_by_name() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_corpus_with_jsonb_facet())
    assert "(facets = '{}'::jsonb OR NOT EXISTS (" in body
    assert "SELECT 1 FROM jsonb_each(facets) AS f(key, vals)" in body
    assert "(d.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))" in body


def _one_surface_schema(*, facet_name: str, collection_facet: str) -> SearchSchema:
    return SearchSchema(
        surfaces=(
            SearchSurface(
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
            ),
        ),
        facets=(Facet(name=facet_name, sql_type="text", default="'some-literal-value'"),),
        collection_facet=collection_facet,
    )


@pytest.mark.parametrize("builder", _BUILDERS)
def test_the_schemas_declared_collection_facet_always_defaults_to_the_session_setting(builder: BaseHybridSearchBuilder) -> None:
    """The facet a schema names as `collection_facet` ignores its own `Facet.default`: the DEFAULT is the session's."""
    schema = _one_surface_schema(facet_name="collection", collection_facet="collection")
    body = builder.api_search_body(schema)
    assert "collection text DEFAULT current_setting('app.collection', true)" in body
    assert "some-literal-value" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_facet_merely_named_collection_keeps_its_own_default_when_the_schema_does_not_declare_it(
    builder: BaseHybridSearchBuilder,
) -> None:
    """The trigger is `SearchSchema.collection_facet`, never the facet's own name."""
    schema = _one_surface_schema(facet_name="collection", collection_facet="")
    body = builder.api_search_body(schema)
    assert "collection text DEFAULT 'some-literal-value'" in body
    assert "current_setting" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_the_collection_facet_role_follows_a_differently_named_facet(builder: BaseHybridSearchBuilder) -> None:
    """A schema may name any facet as its `collection_facet`; the session default follows the role, not a literal name."""
    schema = _one_surface_schema(facet_name="partition", collection_facet="partition")
    body = builder.api_search_body(schema)
    assert "partition text DEFAULT current_setting('app.collection', true)" in body
    assert "some-literal-value" not in body


def test_omitting_the_level_facet_drops_the_level_filter() -> None:
    schema = SearchSchema(
        surfaces=(
            SearchSurface(
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
            ),
        ),
        facets=(Facet("topic", "text"),),
    )
    body = PgTextSearchHybridSearchBuilder().api_search_body(schema)
    assert "= 'doc'" not in body  # no level gate is generated
    assert "doc_bm25 AS " in body  # but the surface's legs are still there
    assert "doc_vec AS " in body


# -- component support + bounded snippets ------------------------------------

_SNIPPET_CASE = "CASE WHEN char_length(source_text) > 480\n     THEN left(source_text, 479) || '…'\n     ELSE source_text\nEND"
"""The bounded-snippet projection both dialects emit, with the source-text expression substituted in."""


def _rendered_snippet_case(source_text: str) -> str:
    """`_SNIPPET_CASE` with its `source_text` placeholder replaced by a real column expression."""
    return _SNIPPET_CASE.replace("source_text", source_text)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_returns_the_component_rank_columns_after_the_fused_score(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert (
        "RETURNS TABLE (id text, hit_level text, page_id text, page_title text, snippet text, score float8, "
        "lexical_rank int, vector_rank int, vector_distance float8, snippet_truncated boolean)"
    ) in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_keyword_legs_tag_themselves_lexical_and_carry_no_distance(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert body.count("'lexical'::text AS leg") == 2  # one per surface
    assert body.count("NULL::float8 AS vector_distance") == 2


@pytest.mark.parametrize("builder", _BUILDERS)
def test_vector_legs_tag_themselves_semantic_and_project_the_cosine_distance(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert body.count("'semantic'::text AS leg") == 2
    query_vec = "$3" if isinstance(builder, ParadeDBHybridSearchBuilder) else "query_vec"
    assert f"(d.embedding <=> {query_vec})::float8 AS vector_distance" in body
    assert f"(c.embedding <=> {query_vec})::float8 AS vector_distance" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_fusion_aggregates_each_legs_rank_with_a_filter(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert "(min(legs.r) FILTER (WHERE legs.leg = 'lexical'))::int" in body
    assert "(min(legs.r) FILTER (WHERE legs.leg = 'semantic'))::int" in body
    assert "min(legs.vector_distance)::float8" in body
    assert "bool_or(legs.snippet_truncated)" in body
    # The fused ordering is untouched: RRF still ranks, and the clamp still bounds.
    assert "sum(1.0 / (60 + legs.r))::float8 AS rrf" in body
    assert "ORDER BY rrf DESC" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_every_leg_bounds_its_snippet_and_says_whether_it_cut(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    for text in ("d.body", "c.body"):
        assert f"{_rendered_snippet_case(text)} AS snippet" in body
        assert f"char_length({text}) > 480 AS snippet_truncated" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_predicate_compares_a_fused_score_distance_or_similarity_to_a_constant(builder: BaseHybridSearchBuilder) -> None:
    """Support is leg presence; nothing in the generated body gates a row on how close it scored."""
    body = builder.api_search_body(_corpus())
    assert "similarity" not in body
    assert not re.search(r"\brrf\b\s*[<>]=?", body)
    assert not re.search(r"\bvector_distance\b\s*[<>]=?", body)
    # The cosine operator only ever ranks or projects, it never feeds a comparison against a literal.
    assert not re.search(r"<=>[^\n]*?[<>]=?\s*-?\d", body)


# -- partitioned keys ------------------------------------------------------------


def _partitioned_corpus() -> SearchSchema:
    """`_corpus()` with every row keyed by its tenant and its id, and the tenant returned as `tenant_name`."""
    schema = _corpus()
    doc, chunk = schema.surfaces
    return SearchSchema(
        surfaces=(
            replace(doc, partition_col="tenant", membership="(d.tenant, d.id) IN (SELECT doc_pool.tenant, doc_pool.id FROM doc_pool)"),
            replace(chunk, partition_col="tenant", join="JOIN doc_pool dp ON dp.tenant = c.tenant AND dp.id = c.doc_id"),
        ),
        pools=schema.pools,
        facets=schema.facets,
        embedding_sql_type=schema.embedding_sql_type,
        level_facet=schema.level_facet,
        partition_result_col="tenant_name",
    )


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_partitioned_schema_returns_the_partition_beside_the_id(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_partitioned_corpus())
    assert "RETURNS TABLE (id text, tenant_name text, hit_level text, page_id text, " in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_every_leg_projects_its_own_partition(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_partitioned_corpus())
    assert body.count("d.id AS id, d.tenant AS partition_key, ") == 2
    assert body.count("c.id AS id, c.tenant AS partition_key, ") == 2
    assert "SELECT doc_bm25.id, doc_bm25.partition_key, doc_bm25.lvl, " in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_fusion_keys_a_row_by_its_partition_and_its_id(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_partitioned_corpus())
    assert "SELECT legs.id,\n       legs.partition_key,\n" in body
    assert "GROUP BY legs.partition_key, legs.id" in body
    assert "GROUP BY legs.id\n" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_an_unpartitioned_schema_renders_no_partition(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_corpus())
    assert "partition_key" not in body
    assert "GROUP BY legs.id\n" in body


def test_a_partitioned_schema_refuses_a_surface_without_a_partition_column() -> None:
    schema = _partitioned_corpus()
    doc, chunk = schema.surfaces
    with pytest.raises(ValueError, match="chunk"):
        SearchSchema(surfaces=(doc, replace(chunk, partition_col="")), partition_result_col="tenant_name")


def test_an_unpartitioned_schema_refuses_a_surface_with_a_partition_column() -> None:
    doc, _chunk = _partitioned_corpus().surfaces
    with pytest.raises(ValueError, match="partition_result_col"):
        SearchSchema(surfaces=(doc,))


def test_the_partition_result_column_cannot_share_a_facets_name() -> None:
    doc, _chunk = _partitioned_corpus().surfaces
    with pytest.raises(ValueError, match="topic"):
        SearchSchema(surfaces=(doc,), facets=(Facet("topic", "text"),), partition_result_col="topic")


# -- vector-only filters -----------------------------------------------------------


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_vector_filter_narrows_the_vector_leg_alone(builder: BaseHybridSearchBuilder) -> None:
    schema = _corpus()
    doc, chunk = schema.surfaces
    filtered = replace(schema, surfaces=(replace(doc, vector_filters=("d.model_ok",)), chunk))

    body = builder.api_search_body(filtered)
    vector_leg = body[body.index("doc_vec AS") : body.index("chunk_bm25 AS")]
    keyword_leg = body[body.index("doc_bm25 AS") : body.index("doc_vec AS")]

    assert "d.model_ok" in vector_leg
    assert "d.model_ok" not in keyword_leg


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_vector_filter_renders_its_facet_placeholder(builder: BaseHybridSearchBuilder) -> None:
    schema = _corpus()
    doc, chunk = schema.surfaces
    filtered = replace(schema, surfaces=(replace(doc, vector_filters=("({topic} IS NULL OR d.ok)",)), chunk))

    body = builder.api_search_body(filtered)
    topic = "$5" if isinstance(builder, ParadeDBHybridSearchBuilder) else "topic"

    assert f"({topic} IS NULL OR d.ok)" in body


# -- tuning ------------------------------------------------------------------------


def _tuned_corpus(*, relation: str = "kb.search_tuning", budget: int = 50, weights: tuple[tuple[str, float], ...] = ()) -> SearchSchema:
    """`_corpus()` reading its numbers from a tuning row, unless the test passes other tuning fields."""
    return replace(_corpus(), tuning=SearchTuning(relation=relation, budget=budget, weights=weights))


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_tuned_search_reads_the_tuning_row_over_its_defaults(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_tuned_corpus())

    assert "coalesce((SELECT t.tuning FROM kb.search_tuning t WHERE t.id = 1), '{}'::jsonb)" in body
    assert '"budget": 50' in body
    assert '"rrf_constant": 60.0' in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_every_tuned_number_is_clamped_by_a_generated_bound(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_tuned_corpus())

    assert "capped_k int := least(greatest(k, 1), least(greatest((tuning ->> 'k_ceiling')::numeric::int, 1), 50));" in body
    assert "leg_budget int := greatest(least(greatest((tuning ->> 'budget')::numeric::int, 1), 200), capped_k);" in body
    assert "rrf_constant float8 := least(greatest((tuning ->> 'rrf_constant')::float8, 1), 1000);" in body
    assert "snippet_chars int := least(greatest((tuning ->> 'snippet_chars')::numeric::int, 80), 2000);" in body
    for surface in ("doc", "chunk"):
        for leg in ("lexical", "semantic"):
            assert (
                f"weight_{surface}_{leg} float8 := "
                f"least(greatest(coalesce((tuning -> 'weights' ->> '{surface}:{leg}')::float8, 1), 0), 10);"
            ) in body


def test_a_tuning_with_no_relation_reads_its_defaults_alone() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus(relation="", budget=80))

    assert "tuning jsonb := '{" in body
    assert '"budget": 80' in body
    assert "SELECT t.tuning" not in body


def test_every_tuned_leg_stops_at_its_budget() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus())

    assert body.count("LIMIT leg_budget") == 4  # two surfaces, a keyword and a semantic leg each
    assert "LIMIT capped_k\n" not in body.split("legs AS (")[0]


def test_a_paradedb_keyword_leg_orders_by_score_and_stops_at_its_budget() -> None:
    body = ParadeDBHybridSearchBuilder().api_search_body(_tuned_corpus())
    doc_keyword = body[body.index("doc_bm25 AS") : body.index("doc_vec AS")]

    assert "ORDER BY paradedb.score(d.id) DESC" in doc_keyword
    assert re.search(r"LIMIT \$\d+", doc_keyword)


def test_a_tuned_semantic_leg_takes_its_nearest_rows_then_ranks_them_by_exact_distance() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus())
    doc_semantic = body[body.index("doc_vec AS") : body.index("chunk_bm25 AS")]

    assert "row_number() OVER (ORDER BY near.vector_distance, near.id) AS r" in doc_semantic
    assert "ORDER BY (d.embedding <=> query_vec) + 0\n    LIMIT leg_budget\n  ) AS near" in doc_semantic


def test_a_tuned_fusion_weighs_each_leg_with_the_tuned_constant() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus())

    assert "sum(legs.w / (rrf_constant + legs.r))::float8 AS rrf" in body
    assert "weight_doc_lexical::float8 AS w" in body
    assert "weight_chunk_semantic::float8 AS w" in body
    assert "doc_bm25.w, " in body


def test_a_tuned_snippet_is_cut_at_the_tuned_length() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus())

    assert "CASE WHEN char_length(d.body) > snippet_chars" in body
    assert "char_length(d.body) > snippet_chars AS snippet_truncated" in body


def test_paradedb_binds_every_tuned_number_by_position() -> None:
    body = ParadeDBHybridSearchBuilder().api_search_body(_tuned_corpus())

    assert "USING query, capped_k, query_vec, level, topic, only_current, leg_budget, rrf_constant, snippet_chars, " in body
    assert "weight_doc_lexical, weight_doc_semantic, weight_chunk_lexical, weight_chunk_semantic;" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_placeholder_leaks_into_a_tuned_body(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_tuned_corpus())
    inner = body[body.index("BEGIN") :]

    assert "{" not in inner.replace("'{}'::jsonb", "")


def test_a_weight_for_no_surface_and_leg_of_the_schema_is_refused() -> None:
    with pytest.raises(ValueError, match="page:lexical"):
        _tuned_corpus(weights=(("page:lexical", 2.0),))


def test_a_weight_names_a_surface_and_a_leg() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_tuned_corpus(weights=(("doc:semantic", 2.5),)))

    assert '"doc:semantic": 2.5' in body


def test_an_untuned_schema_keeps_its_constants() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_corpus())

    assert "capped_k int := least(greatest(k, 1), 50);" in body
    assert "sum(1.0 / (60 + legs.r))::float8 AS rrf" in body
    assert "leg_budget" not in body


def test_a_facet_named_like_a_tuned_variable_is_refused() -> None:
    schema = _corpus()
    with pytest.raises(ValueError, match="leg_budget"):
        replace(schema, facets=(*schema.facets, Facet("leg_budget", "int")), tuning=SearchTuning())


# -- policies ----------------------------------------------------------------------


def _pinned_corpus() -> SearchSchema:
    """`_tuned_corpus()` whose top-level docs pin on an exact title and whose chunks group by their doc."""
    schema = _tuned_corpus()
    doc, chunk = schema.surfaces
    return replace(schema, surfaces=(replace(doc, title_pin=True), chunk))


def test_a_tuned_search_caps_the_rows_of_one_group_after_the_fusion() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_pinned_corpus())

    assert "rows_per_group int := least(greatest((tuning ->> 'rows_per_group')::numeric::int, 1), 50);" in body
    assert "row_number() OVER (PARTITION BY fused.grp, fused.pin_alone ORDER BY fused.rrf DESC, fused.id) AS in_group" in body
    assert "WHERE ruled.pin_alone OR ruled.in_group <= rows_per_group" in body
    assert "ORDER BY ruled.pin_alone DESC, ruled.rrf DESC, ruled.id" in body


def test_each_leg_names_its_group_by_its_parent_or_its_own_id() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_pinned_corpus())

    assert "coalesce(d.id, d.id)::text AS grp" in body
    assert "coalesce(c.doc_id, c.id)::text AS grp" in body


def test_a_surface_may_group_by_its_own_expression() -> None:
    schema = _pinned_corpus()
    doc, chunk = schema.surfaces
    body = PgTextSearchHybridSearchBuilder().api_search_body(replace(schema, surfaces=(doc, replace(chunk, group_expr="c.section"))))

    assert "coalesce(c.section, c.id)::text AS grp" in body


def test_only_a_pinning_surface_compares_its_title_with_the_query() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_pinned_corpus())

    assert body.count("lower(btrim(d.title)) = lower(btrim(query)) AS pin") == 2
    assert body.count("false AS pin") == 2


def test_a_title_pins_only_when_one_candidate_holds_it() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_pinned_corpus())

    assert "legs_fused.pin AND count(*) FILTER (WHERE legs_fused.pin) OVER () = 1 AS pin_alone" in body


def test_a_partitioned_tuned_search_groups_within_each_partition() -> None:
    schema = _partitioned_corpus()
    tuned = replace(schema, tuning=SearchTuning(relation="kb.search_tuning"))
    body = PgTextSearchHybridSearchBuilder().api_search_body(tuned)

    assert "PARTITION BY fused.partition_key, fused.grp, fused.pin_alone" in body


def test_an_untuned_search_applies_no_policy() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_corpus())

    assert "pin_alone" not in body
    assert "AS grp" not in body


# -- documents ---------------------------------------------------------------------


def _documented_corpus(*, partitioned: bool = False) -> SearchSchema:
    """`_pinned_corpus()` keeping the best hit of each doc, a chunk counting against its doc, or the partitioned corpus capped alike."""
    schema = _partitioned_corpus() if partitioned else _pinned_corpus()
    doc, chunk = schema.surfaces
    return replace(
        schema,
        surfaces=(doc, replace(chunk, document_expr="'doc:' || c.doc_id")),
        tuning=SearchTuning(relation="kb.search_tuning", max_hits_per_document=1),
    )


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_capped_search_reads_its_cap_from_the_tuning_row_with_no_cap_below_one(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_documented_corpus())

    assert '"max_hits_per_document": 1' in body
    assert "max_hits_per_document int := least(greatest(coalesce((tuning ->> 'max_hits_per_document')::numeric::int, 0), 0), 50);" in body


def test_each_leg_names_its_document_by_its_own_expression_or_its_group() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_documented_corpus())

    assert body.count("coalesce(d.id, d.id)::text AS document_key") == 2
    assert body.count("coalesce('doc:' || c.doc_id, c.id)::text AS document_key") == 2
    assert "doc_bm25.document_key, " in body


def test_each_document_ranks_the_hits_the_group_rule_kept_a_lone_title_first() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_documented_corpus())

    assert "max(legs.document_key) AS document_key" in body
    assert (
        "row_number() OVER (PARTITION BY ruled.document_key ORDER BY ruled.pin_alone DESC, ruled.rrf DESC, ruled.id) AS in_document"
    ) in body
    assert "WHERE ruled.pin_alone OR ruled.in_group <= rows_per_group" in body


def test_a_document_keeps_its_best_hits_up_to_the_cap_and_a_cap_of_zero_keeps_every_hit() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_documented_corpus())

    assert "WHERE max_hits_per_document = 0 OR documented.in_document <= max_hits_per_document" in body
    assert "ORDER BY documented.pin_alone DESC, documented.rrf DESC, documented.id\nLIMIT capped_k" in body


def test_the_hits_past_the_cap_fold_under_the_best_hit_of_their_document() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_documented_corpus())

    assert "jsonb_agg(jsonb_build_object('id', documented.id, 'hit_level', documented.lvl) ORDER BY documented.in_document) AS units" in body
    assert "WHERE max_hits_per_document > 0 AND documented.in_document > max_hits_per_document" in body
    assert f"documented.in_document <= max_hits_per_document + {FOLDED_UNITS_PER_HIT}" in body
    assert "LEFT JOIN folded ON folded.document_key = documented.document_key AND documented.in_document = 1" in body
    assert "coalesce(folded.units, '[]'::jsonb)" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_capped_search_returns_the_folded_units_last(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_documented_corpus())

    assert "vector_distance float8, snippet_truncated boolean, folded_units jsonb)" in body


def test_a_partitioned_capped_search_keys_each_document_by_its_partition() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_documented_corpus(partitioned=True))

    assert "PARTITION BY ruled.partition_key, ruled.document_key ORDER BY" in body
    assert "GROUP BY documented.partition_key, documented.document_key" in body
    assert "folded.partition_key = documented.partition_key AND folded.document_key = documented.document_key" in body


def test_paradedb_binds_the_cap_by_position() -> None:
    body = ParadeDBHybridSearchBuilder().api_search_body(_documented_corpus())

    assert "weight_chunk_lexical, weight_chunk_semantic, max_hits_per_document;" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_placeholder_leaks_into_a_capped_body(builder: BaseHybridSearchBuilder) -> None:
    body = builder.api_search_body(_documented_corpus())
    inner = body[body.index("BEGIN") :]

    assert "{" not in inner.replace("'{}'::jsonb", "")


def test_a_tuned_search_without_a_cap_keeps_every_document_hit_and_folds_none() -> None:
    body = PgTextSearchHybridSearchBuilder().api_search_body(_pinned_corpus())

    assert "max_hits_per_document" not in body
    assert "document_key" not in body
    assert "folded_units" not in body


def test_a_facet_named_like_the_cap_is_refused() -> None:
    schema = _corpus()
    with pytest.raises(ValueError, match="max_hits_per_document"):
        replace(schema, facets=(*schema.facets, Facet("max_hits_per_document", "int")), tuning=SearchTuning(max_hits_per_document=1))
