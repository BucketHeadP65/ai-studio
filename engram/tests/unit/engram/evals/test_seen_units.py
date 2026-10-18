"""The unit ids the tool results of one agent run named, read from the run's own messages."""

from __future__ import annotations

import json

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


def _run_code(content: object, *returns: tuple[str, object], metadata: object = None) -> list[ModelMessage]:
    """One `run_code` return carrying `content` and, unless `metadata` is given, its program's tool returns as code mode records them."""
    nested = {
        f"run-code__{index}": ToolReturnPart(tool_name=name, content=returned, tool_call_id=f"run-code__{index}")
        for index, (name, returned) in enumerate(returns, start=1)
    }
    recorded = metadata if metadata is not None else {"code_mode": True, "tool_calls": {}, "tool_returns": nested}
    return [ModelRequest(parts=[ToolReturnPart(tool_name="run_code", content=content, tool_call_id="run-code", metadata=recorded)])]


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


def test_a_graph_answer_names_each_node_and_never_a_provenance_claim_it_only_points_at():
    expanded = GraphResult(
        nodes=(_node("note.alpha"), _node("note.beta")),
        edges=(GraphEdge(src_id="note.alpha", dst_id="note.beta", rel="cites", provenance_claim="note.alpha#c1"),),
    )
    routes = GraphPathsResult(
        paths=(GraphPath(nodes=(_node("note.gamma"), _node("note.delta")), edges=(GraphEdge(src_id="note.gamma", dst_id="note.delta", rel="cites"),), score=1.0),),
        next_steps=_NEXT,
    )

    seen = seen_units(_returned(("graph_expand", expanded), ("find_paths", routes)), collection="notes")

    assert seen == ("note.alpha", "note.beta", "note.gamma", "note.delta")


def test_a_sql_row_names_its_own_unit_columns_and_never_the_other_end_of_a_relation():
    rows = {
        "rows": [
            {"source_id": "src-alpha", "title": "src-gamma", "src_id": "note.beta"},
            {"id": "note.delta", "body": "See note.epsilon for more.", "dst_id": "note.zeta", "subject_id": "note.eta"},
            {"claim_id": "note.theta#c1", "quote": "Measured on two datasets.", "page_id": "note.iota", "section_id": "note.kappa#s1"},
        ],
        "note": None,
    }

    seen = seen_units(_returned(("run_sql", rows)), collection="notes")

    assert seen == ("src-alpha", "note.delta", "note.theta#c1", "note.iota", "note.kappa#s1")


def test_a_sql_row_that_holds_only_relation_ends_names_no_unit():
    rows = {"rows": [{"src_id": "note.alpha", "dst_id": "note.beta", "rel": "cites", "subject_id": "note.alpha"}], "note": None}

    assert seen_units(_returned(("run_sql", rows)), collection="notes") == ()


def test_a_run_code_result_keyed_by_a_number_never_raises_and_names_nothing():
    assert seen_units(_returned(("run_code", {1: "note.alpha"})), collection="notes") == ()


def test_a_key_that_is_not_text_never_raises():
    rows = {"rows": [{1: "note.alpha", "page_id": "note.beta"}], "note": None}

    assert seen_units(_run_code({2: "note.beta", 3: 4}, ("run_sql", rows)), collection="notes") == ("note.beta",)


def test_a_run_code_output_never_names_a_unit_no_program_tool_call_returned():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),), next_steps=_NEXT)
    fetched = FetchResult(id="note.beta", level="card", body="Builds on [[note.zeta]] and the chunk src-eta:2.")
    output = {"output": "saw note.alpha#s1 and page:notes/note.delta.\n", "result": {"source_id": "src-epsilon", "body": fetched.body}}

    seen = seen_units(_run_code(output, ("search", search), ("fetch", fetched)), collection="notes")

    assert seen == ("note.alpha#s1", "note.beta")


def test_a_run_code_output_carries_a_returned_unit_by_its_ref_or_as_a_key():
    search = SearchResult(
        hits=(
            SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),
            SearchHit(id="note.gamma", hit_level="card", score=0.5),
        ),
        next_steps=_NEXT,
    )
    fetched = FetchResult(id="note.beta", level="card", body="The body.")
    output = {"output": "see section:notes/note.alpha#s1.\n", "result": {"note.beta": "The body."}}

    seen = seen_units(_run_code(output, ("search", search), ("fetch", fetched)), collection="notes")

    assert seen == ("note.alpha#s1", "note.beta")


def test_a_run_code_output_that_holds_only_a_count_names_no_unit_its_program_received():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),), next_steps=_NEXT)

    assert seen_units(_run_code(1, ("search", search)), collection="notes") == ()


_ALPHA_BODY = "Day two of the vote count. It follows up on [[note.beta]] from the day before, which set the date of the count."
_ALPHA_SNIPPET = "The second day of the vote count in the northern district."
_BETA_SNIPPET = "The date of the vote count was set for the end of the month."


def test_a_link_inside_a_printed_body_never_counts_even_when_the_linked_unit_was_returned():
    search = SearchResult(
        hits=(
            SearchHit(id="note.alpha", hit_level="card", snippet=_ALPHA_SNIPPET, score=1.0),
            SearchHit(id="note.beta", hit_level="card", snippet=_BETA_SNIPPET, score=0.5),
        ),
        next_steps=_NEXT,
    )
    fetched = FetchResult(id="note.alpha", level="card", body=_ALPHA_BODY)

    seen = seen_units(_run_code({"output": f"{_ALPHA_BODY}\n"}, ("search", search), ("fetch", fetched)), collection="notes")

    assert seen == ("note.alpha",)


def test_a_body_printed_without_its_id_counts_for_the_unit_it_belongs_to():
    body = "Line one of the report.\n\n  It spans   several lines and keeps going well past sixty characters in all."
    fetched = FetchResult(id="note.beta", level="card", body=body)

    printed = seen_units(_run_code({"output": f"{body[:90]}\n"}, ("fetch", fetched)), collection="notes")
    in_a_printed_dict = seen_units(_run_code({"output": f"{ {'body': body}!r}\n"}, ("fetch", fetched)), collection="notes")

    assert (printed, in_a_printed_dict) == (("note.beta",), ("note.beta",))


def test_a_unit_an_earlier_run_code_call_returned_counts_when_a_later_call_prints_it():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", snippet=_ALPHA_SNIPPET, score=1.0),), next_steps=_NEXT)

    seen = seen_units([*_run_code(1, ("search", search)), *_run_code({"output": f"note.alpha#s1 {_ALPHA_SNIPPET}\n"})], collection="notes")

    assert seen == ("note.alpha#s1",)


def test_an_id_written_after_an_equals_sign_counts():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),), next_steps=_NEXT)

    assert seen_units(_run_code({"output": "id=note.alpha#s1 score=1.0\n"}, ("search", search)), collection="notes") == ("note.alpha#s1",)


def test_a_returned_unit_counts_through_its_page_id_printed_with_its_snippet():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", snippet=_ALPHA_SNIPPET, score=1.0),), next_steps=_NEXT)

    seen = seen_units(_run_code({"output": f"note.alpha: {_ALPHA_SNIPPET}\n"}, ("search", search)), collection="notes")

    assert seen == ("note.alpha#s1",)


def test_a_returned_unit_counts_through_its_page_id_alone():
    search = SearchResult(hits=(SearchHit(id="note.alpha#s1", hit_level="section", page_id="note.alpha", score=1.0),), next_steps=_NEXT)

    assert seen_units(_run_code({"output": "pages: note.alpha\n"}, ("search", search)), collection="notes") == ("note.alpha#s1",)


_QUOTED_BODY = 'The company\'s chief said "growth" would return by the spring. It follows up on [[note.beta]] from the café.'


def _quoted_body_returns() -> tuple[tuple[str, object], ...]:
    """A search that returned note.alpha and note.beta, then a fetch of note.alpha whose body holds both quote marks and a link."""
    search = SearchResult(
        hits=(
            SearchHit(id="note.alpha", hit_level="card", snippet=_ALPHA_SNIPPET, score=1.0),
            SearchHit(id="note.beta", hit_level="card", snippet=_BETA_SNIPPET, score=0.5),
        ),
        next_steps=_NEXT,
    )
    return ("search", search), ("fetch", FetchResult(id="note.alpha", level="card", title="Alpha", body=_QUOTED_BODY))


def test_a_body_printed_as_a_python_value_is_matched_and_cut_through_its_escaped_quotes():
    printed = {"id": "note.alpha", "level": "card", "title": "Alpha", "body": _QUOTED_BODY}

    assert seen_units(_run_code({"output": f"{printed!r}\n"}, *_quoted_body_returns()), collection="notes") == ("note.alpha",)


def test_a_unit_printed_as_json_is_matched_and_cut_through_its_escaped_quotes_and_characters():
    printed = {"id": "note.alpha", "level": "card", "title": "Alpha", "body": _QUOTED_BODY}

    assert seen_units(_run_code({"output": f"{json.dumps(printed)}\n"}, *_quoted_body_returns()), collection="notes") == ("note.alpha",)


def test_a_body_printed_as_a_json_string_counts_its_unit_and_never_its_link():
    output = {"output": f"{json.dumps(_QUOTED_BODY)}\n"}

    assert seen_units(_run_code(output, *_quoted_body_returns()), collection="notes") == ("note.alpha",)


def test_a_returned_text_is_cut_whatever_field_it_came_in_before_ids_are_read():
    search = SearchResult(hits=(SearchHit(id="note.beta", hit_level="card", snippet=_BETA_SNIPPET, score=1.0),), next_steps=_NEXT)
    rows = {"rows": [{"body": _ALPHA_BODY}], "note": None}

    seen = seen_units(_run_code({"output": f"{_ALPHA_BODY}\n"}, ("search", search), ("run_sql", rows)), collection="notes")

    assert seen == ()


def test_a_claim_counts_when_the_result_carried_its_text():
    hits = SearchResult(hits=(SearchHit(id="note.alpha#c1", hit_level="claim", page_id="note.alpha", snippet="Graphs help recall.", score=1.0),), next_steps=_NEXT)
    fetched = FetchResult(id="note.beta#c2", level="claim", body="Caching halves the latency.")
    rows = {
        "rows": [
            {"claim_id": "note.gamma#c3", "text": "Reranking adds four points."},
            {"id": "note.delta#c4", "hit_level": "claim", "snippet": "A claim's words."},
        ],
        "note": None,
    }

    seen = seen_units(_returned(("search", hits), ("fetch", fetched), ("run_sql", rows)), collection="notes")

    assert seen == ("note.alpha#c1", "note.beta#c2", "note.gamma#c3", "note.delta#c4")


def test_a_claim_id_that_came_without_its_text_names_no_unit():
    hits = SearchResult(hits=(SearchHit(id="note.alpha#c1", hit_level="claim", page_id="note.alpha", score=1.0),), next_steps=_NEXT)
    fetched = FetchResult(id="note.beta#c2", level="claim", body=None)
    rows = {"rows": [{"claim_id": "note.gamma#c3"}, {"id": "note.delta#c4", "hit_level": "claim", "snippet": " "}], "note": None}
    pointed = GraphResult(edges=(GraphEdge(src_id="note.epsilon", dst_id="note.zeta", rel="cites", provenance_claim="note.eta#c5"),))

    seen = seen_units(_returned(("search", hits), ("fetch", fetched), ("run_sql", rows), ("graph_expand", pointed)), collection="notes")

    assert seen == ()


def test_a_run_code_call_whose_program_received_no_tool_return_names_no_unit():
    no_record = _run_code({"source_id": "src-alpha"}, metadata={"code_mode": True})
    denied = _run_code("note.alpha", ("fetch", "The fetch tool was denied for note.alpha."))
    unreadable = _run_code("note.alpha", metadata=["not", "a", "record"])

    assert [seen_units(messages, collection="notes") for messages in (no_record, denied, unreadable)] == [(), (), ()]


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
