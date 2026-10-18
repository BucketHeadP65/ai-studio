"""The unit ids the tool results of one agent run named, read from the run's own messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope
from core.ai.retrieval.contracts import (
    FetchResult,
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphPathsResult,
    GraphResult,
    NextSteps,
    SearchHit,
    SearchResult,
)
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from engram.evals.harness.seen_units import seen_units

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

_NEXT = NextSteps(summary="What this result is.")


def _returned(*returns: tuple[str, object]) -> list[ModelMessage]:
    """One request carrying a tool return per `(tool name, content)` pair, in order."""
    return [
        ModelRequest(
            parts=[ToolReturnPart(tool_name=name, content=content, tool_call_id=f"call-{index}") for index, (name, content) in enumerate(returns)]
        )
    ]


def _node(page_id: str) -> GraphNode:
    return GraphNode(id=page_id, type="note", title=page_id, status="active", superseded=False)


def test_a_search_names_each_hit():
    result = SearchResult(
        hits=(
            SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),
            SearchHit(id="src-beta:3", hit_level="raw", score=0.5),
        ),
        next_steps=_NEXT,
    )

    assert seen_units(_returned(("search", result)), collection="notes") == ("note.alpha#s1", "src-beta:3")


def test_a_fetch_names_the_unit_it_read():
    assert seen_units(_returned(("fetch", FetchResult(id="note.gamma", level="card"))), collection="notes") == ("note.gamma",)


def test_a_leveled_fetch_names_its_unit_and_every_child_it_lists():
    page = DisclosureEnvelope(
        ref="page:notes/note.alpha",
        kind="note",
        title="Alpha",
        children=(ChildSummary(ref="section:notes/note.alpha#s2", kind="section", title="Setup", summary="The setup."),),
    )
    root = DisclosureEnvelope(
        ref="collection:notes", kind="collection", title="Notes", children=(ChildSummary(ref="page:notes/note.beta", kind="note", title="Beta", summary="B."),)
    )

    assert seen_units(_returned(("fetch", page), ("fetch", root)), collection="notes") == ("note.alpha", "note.alpha#s2", "note.beta")


def test_a_graph_answer_names_each_node_and_each_provenance_claim():
    expanded = GraphResult(
        nodes=(_node("note.alpha"), _node("note.beta")),
        edges=(GraphEdge(src_id="note.alpha", dst_id="note.beta", rel="cites", provenance_claim="note.alpha#c1"),),
    )
    routes = GraphPathsResult(
        paths=(GraphPath(nodes=(_node("note.gamma"), _node("note.delta")), edges=(GraphEdge(src_id="note.gamma", dst_id="note.delta", rel="cites"),), score=1.0),),
        next_steps=_NEXT,
    )

    seen = seen_units(_returned(("graph_expand", expanded), ("find_paths", routes)), collection="notes")

    assert seen == ("note.alpha", "note.beta", "note.alpha#c1", "note.gamma", "note.delta")


def test_a_sql_row_names_each_column_called_id_or_ending_in_id_and_nothing_else():
    rows = {
        "rows": [
            {"source_id": "src-alpha", "title": "src-gamma", "src_id": "note.beta"},
            {"id": "note.delta", "body": "See note.epsilon for more."},
        ],
        "note": None,
    }

    assert seen_units(_returned(("run_sql", rows)), collection="notes") == ("src-alpha", "note.beta", "note.delta")


def test_a_run_code_output_is_read_whole_as_the_model_reads_it():
    output = {
        "output": "saw note.alpha#s1 and page:notes/note.beta.\n",
        "result": [{"source_id": "src-delta", "title": "Plain words only"}, {"snippet": "cut from src-gamma:2, a chunk"}],
    }

    seen = seen_units(_returned(("run_code", output)), collection="notes")

    assert seen == ("note.alpha#s1", "note.beta", "src-delta", "src-gamma:2")


def test_a_run_code_output_that_holds_no_id_names_no_unit():
    assert seen_units(_returned(("run_code", 3), ("run_code", "two hits, both relevant")), collection="notes") == ()


def test_a_schema_description_and_the_answer_name_no_unit():
    returns = (("describe_schema", "- api.cards (view): One card per page."), ("final_result", "Final result processed."))

    assert seen_units(_returned(*returns), collection="notes") == ()


def test_the_ids_a_model_asks_for_are_not_results():
    messages: list[ModelMessage] = [ModelResponse(parts=[ToolCallPart("fetch", {"unit_id": "note.alpha"})])]

    assert seen_units(messages, collection="notes") == ()


def test_each_unit_is_named_once_in_the_order_the_model_received_it():
    first = SearchResult(hits=(SearchHit(id="note.beta", hit_level="card", score=1.0), SearchHit(id="note.alpha", hit_level="card", score=0.5)), next_steps=_NEXT)
    second = SearchResult(hits=(SearchHit(id="note.alpha", hit_level="card", score=1.0), SearchHit(id="note.gamma", hit_level="card", score=0.5)), next_steps=_NEXT)

    assert seen_units([*_returned(("search", first)), *_returned(("search", second))], collection="notes") == ("note.beta", "note.alpha", "note.gamma")
