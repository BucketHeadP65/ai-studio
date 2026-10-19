from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pydantic import ValidationError

from core.ai.retrieval import names
from core.ai.retrieval.contracts import (
    Fetch,
    FetchResult,
    FoldedUnit,
    GraphEdge,
    GraphExpand,
    GraphNode,
    GraphPath,
    GraphPaths,
    GraphPathsResult,
    GraphResult,
    KnowledgeBaseStamp,
    NextSteps,
    ResultWindow,
    Search,
    SearchHit,
    SearchResult,
    SqlResult,
)
from core.cqrs.contracts import Message


def test_generic_search_core_carries_no_facets() -> None:
    # The whole point of the split: the generic Search is query / k / hybrid only. A knowledge base's facets
    # (level / task / ...) are added by a consumer subclass, never baked into the library core.
    assert set(Search.model_fields) == {"query", "k", "hybrid"}


def test_queries_are_frozen_and_never_bus_messages() -> None:
    for query in (Search, Fetch, GraphExpand, GraphPaths):
        assert not issubclass(query, Message)
        assert query.model_config.get("frozen") is True


def test_search_defaults() -> None:
    search = Search(query="graph retrieval")
    assert search.k == 12
    assert search.hybrid is True


def test_graph_expand_defaults() -> None:
    expand = GraphExpand(seed_ids=("a", "b"))
    assert expand.hops == 2
    assert expand.rel_types is None
    assert expand.k == 20


def test_graph_paths_defaults() -> None:
    paths = GraphPaths(from_id="a", to_id="b")
    assert paths.max_hops == 4
    assert paths.k == 10


def test_search_hit_optional_fields_default_none() -> None:
    hit = SearchHit(id="p1", hit_level="doc", score=1.5)
    assert hit.page_id is None
    assert hit.page_title is None
    assert hit.snippet is None
    assert hit.answered_by is None
    assert hit.knowledge_base is None


def test_read_result_refs_and_an_unmeasured_stamp_default_to_none() -> None:
    """Old persisted result rows remain valid while routed readers gain exact fetch handles."""
    assert SearchHit(id="paper.x", hit_level="card", score=0.5).ref is None
    assert FetchResult(id="paper.x#c1", level="claim").ref is None
    assert GraphNode(id="paper.x", type="paper", title="Paper", status="active", superseded=False).ref is None
    assert GraphEdge(src_id="a", dst_id="b", rel="cites").provenance_ref is None
    assert KnowledgeBaseStamp(slug="db-only").changed_since is None


def test_search_hit_still_validates_a_row_with_no_answered_by_key() -> None:
    # `SearchHit.model_validate(dict(row))` is how every search handler projects a DB row; the
    # field must stay defaulted so a row that never carried this column keeps validating.
    row = {"id": "p1", "hit_level": "doc", "page_id": None, "page_title": None, "snippet": None, "score": 1.5}
    hit = SearchHit.model_validate(row)
    assert hit.answered_by is None
    assert hit.knowledge_base is None


def test_a_search_hit_folds_no_units_unless_its_row_lists_some() -> None:
    assert SearchHit(id="p1", hit_level="card", score=1.5).folded_units == ()


def test_a_search_hit_reads_its_folded_units_best_first_from_its_row() -> None:
    row = {
        "id": "p1",
        "hit_level": "card",
        "score": 1.5,
        "folded_units": [{"id": "p1#s2", "hit_level": "section"}, {"id": "src.a:3", "hit_level": "raw"}],
    }

    hit = SearchHit.model_validate(row)

    assert hit.folded_units == (FoldedUnit(id="p1#s2", hit_level="section"), FoldedUnit(id="src.a:3", hit_level="raw"))
    assert hit.folded_units[0].ref is None


def test_a_folded_unit_reads_the_snippet_its_row_carries() -> None:
    row = {"id": "p1", "hit_level": "card", "score": 1.5, "folded_units": [{"id": "p1#s2", "hit_level": "section", "snippet": "The cache keeps…"}]}

    hit = SearchHit.model_validate(row)

    assert hit.folded_units[0].snippet == "The cache keeps…"


def test_a_folded_unit_from_a_row_listing_no_snippet_has_none() -> None:
    assert FoldedUnit(id="p1#s2", hit_level="section").snippet is None


def test_search_hit_carries_which_knowledge_base_it_came_from() -> None:
    hit = SearchHit(id="p1", hit_level="doc", score=1.5, knowledge_base="repo-docs")
    assert hit.knowledge_base == "repo-docs"


def test_graph_edge_provenance_is_optional() -> None:
    assert GraphEdge(src_id="a", dst_id="b", rel="cites").provenance_claim is None


def test_sql_result_defaults_to_empty_and_untruncated() -> None:
    result = SqlResult()
    assert result.rows == ()
    assert result.columns == ()
    assert result.truncated_message is None


def test_next_steps_options_default_to_empty() -> None:
    next_steps = NextSteps(summary="No hits.")
    assert next_steps.options == ()


def test_next_steps_is_frozen() -> None:
    next_steps = NextSteps(summary="No hits.")
    with pytest.raises(ValidationError):
        next_steps.summary = "Changed."


def test_search_result_carries_hits_and_next_steps() -> None:
    hit = SearchHit(id="p1", hit_level="card", score=1.5)
    result = SearchResult(hits=(hit,), next_steps=NextSteps(summary="1 hit."))
    assert result.hits == (hit,)
    assert result.next_steps.summary == "1 hit."


def test_search_result_hits_default_to_empty() -> None:
    result = SearchResult(next_steps=NextSteps(summary="No hits."))
    assert result.hits == ()


def test_a_corpus_stamp_names_the_corpus_and_how_far_it_moved_since_its_compile() -> None:
    stamp = KnowledgeBaseStamp(slug="repo-docs", indexed_at=datetime(2026, 8, 26, 23, 50, tzinfo=UTC), changed_since=3)

    assert stamp.slug == "repo-docs"
    assert stamp.changed_since == 3


def test_a_never_compiled_corpus_stamps_with_no_time_and_an_unmeasured_change_count() -> None:
    stamp = KnowledgeBaseStamp(slug="fresh")

    assert stamp.indexed_at is None
    assert stamp.changed_since is None


def test_a_stamp_never_counts_below_zero() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseStamp(slug="repo-docs", changed_since=-1)


def test_results_carry_no_stamp_until_a_host_attaches_one() -> None:
    assert SearchResult(next_steps=NextSteps(summary="two hits")).knowledge_base is None
    assert FetchResult(id="page.x", level="page").knowledge_base is None


def test_an_attached_stamp_rides_the_result_unchanged() -> None:
    stamp = KnowledgeBaseStamp(slug="repo-docs", changed_since=1)

    assert SearchResult(next_steps=NextSteps(summary="two hits"), knowledge_base=stamp).knowledge_base == stamp
    assert FetchResult(id="page.x", level="page", knowledge_base=stamp).knowledge_base == stamp


# -- ResultWindow / search support / GraphPathsResult (focused retrieval) ---


def test_truncated_window_can_admit_unknown_total():
    window = ResultWindow(returned_items=20, omitted_items=None, truncated=True, reason="More nodes exist. Narrow relation types.")
    assert window.omitted_items is None


def test_component_support_is_not_called_grounding():
    hit = SearchHit(id="p", hit_level="card", score=0.1, lexical_rank=1, vector_rank=3, vector_distance=0.22, support="lexical_and_semantic")
    result = SearchResult(hits=(hit,), status="lexically_supported_candidates", next_steps=NextSteps(summary="Candidates. Evidence unverified."))
    assert result.status == "lexically_supported_candidates"
    assert "ground" not in result.status


def test_budget_refusal_is_result_shaped():
    window = ResultWindow(returned_items=0, omitted_items=None, truncated=True, refused=True, reason="Narrow search and try again.")
    result = SearchResult(status="budget_refused", hits=(), window=window, next_steps=NextSteps(summary="Narrow search and try again."))
    assert len(result.model_dump_json()) < 12_000


def test_graph_paths_is_a_result_not_a_bare_list():
    result = GraphPathsResult(paths=(), next_steps=NextSteps(summary="No path found."))
    assert result.window == ResultWindow()


def test_a_window_never_counts_returned_items_below_zero() -> None:
    with pytest.raises(ValidationError):
        ResultWindow(returned_items=-1)


def test_a_window_never_counts_omitted_items_below_zero() -> None:
    with pytest.raises(ValidationError):
        ResultWindow(omitted_items=-1)


def test_a_refused_window_must_carry_zero_returned_items() -> None:
    with pytest.raises(ValidationError):
        ResultWindow(returned_items=1, refused=True, reason="Narrow the query.")


def test_a_refused_window_must_name_a_next_call() -> None:
    with pytest.raises(ValidationError):
        ResultWindow(returned_items=0, refused=True, reason=None)
    with pytest.raises(ValidationError):
        ResultWindow(returned_items=0, refused=True, reason="   ")


def test_result_window_defaults_to_complete_and_unrefused() -> None:
    window = ResultWindow()
    assert (window.returned_items, window.omitted_items, window.truncated, window.refused, window.reason) == (0, 0, False, False, None)


def test_search_hit_component_fields_default_to_none_and_untruncated() -> None:
    hit = SearchHit(id="p1", hit_level="doc", score=1.5)
    assert hit.snippet_truncated is False
    assert hit.lexical_rank is None
    assert hit.vector_rank is None
    assert hit.vector_distance is None
    assert hit.support is None


def test_search_result_new_fields_default_to_unassessed_and_a_complete_window() -> None:
    result = SearchResult(next_steps=NextSteps(summary="No hits."))
    assert result.status == "unassessed"
    assert result.window == ResultWindow()


def test_graph_result_new_fields_default_to_none_and_a_complete_window() -> None:
    result = GraphResult()
    assert result.window == ResultWindow()
    assert result.knowledge_base is None
    assert result.next_steps is None


def test_graph_path_constructor_is_unchanged() -> None:
    path = GraphPath(nodes=(), edges=(), score=0.9)
    assert path.score == 0.9


def test_graph_paths_result_defaults_to_no_stamp_and_a_complete_window() -> None:
    result = GraphPathsResult(paths=(), next_steps=NextSteps(summary="No path found."))
    assert result.paths == ()
    assert result.knowledge_base is None


def test_vector_distance_description_is_within_list_only_never_a_relevance_claim() -> None:
    description = SearchHit.model_json_schema()["properties"]["vector_distance"]["description"]
    assert "among hits in this result list" in description
    assert "relevance across" not in description


def test_the_names_module_holds_only_the_tool_names_and_the_retry_budget() -> None:
    constants = {name for name in vars(names) if name.isupper()}
    assert constants == {"SEARCH", "FETCH", "GRAPH_EXPAND", "FIND_PATHS", "TOOL_NAMES", "RUN_CODE", "RUN_SQL", "TOOL_MAX_RETRIES"}
