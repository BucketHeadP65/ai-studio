"""The views: the run in one screen, a slice of it, a search over it, and what a lead opens on."""

from __future__ import annotations

import re

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pydantic_ai.messages import (
    CompactionPart,
    ImageUrl,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    RetryFeedbackPart,
    RetryInfo,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.tools import ToolDefinition

from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.views import (
    FIND_LIMIT,
    WINDOW,
    StepOutline,
    find,
    label_of,
    outline,
    outline_steps,
    parse_span,
    plain_whys,
    read,
    rendered,
    runs_summary,
    shown,
    step_at,
    text_of,
    turn_at,
    turns_for,
    view_for,
    with_refusal,
)
from core.ai.transcripts.contracts import RunSummary, Span, Step, Transcript
from tests.support.fakes.inspection import AT, answer, call, question, result, retried, transcript

if TYPE_CHECKING:
    from pydantic_ai.messages import ToolRetryCause

EARLIER = RunSummary(
    source="record:run-0",
    agent="harness-author",
    started_at=datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
    first_prompt="index the notes",
    last_event_type="run_failed",
)


def a_run():
    return transcript(
        question(),
        call("c1"),
        result("c1", "sunny and mild"),
        call("c2"),
        retried("c2"),
        call("c3"),
        retried("c3", "no city named Atlantis", cause="model_retry"),
        answer("done"),
    )


def test_the_outline_gives_one_line_per_step_with_what_was_asked_called_and_said() -> None:
    lines = outline(a_run()).splitlines()

    assert len(lines) == 1
    assert lines[0].startswith("step 0: ")
    assert "asked 'What is the weather in Paris?'" in lines[0]
    assert "called lookup_city (answered)" in lines[0]
    assert "called lookup_city (rejected by the schema)" in lines[0]
    assert "called lookup_city (refused by the tool)" in lines[0]
    assert "said 'done'" in lines[0]


@pytest.mark.parametrize(
    ("cause", "ended"),
    [("unknown_tool", "an unknown tool"), ("unavailable_tool", "not available yet"), (None, "asked again")],
)
def test_the_outline_says_why_a_call_has_to_be_made_again(cause: ToolRetryCause | None, ended: str) -> None:
    given = transcript(question(), call("c1"), retried("c1", "try again", cause=cause))

    assert f"called lookup_city ({ended})" in outline(given)


def test_a_retried_return_reads_as_a_retry_with_its_cause() -> None:
    rendered_run = rendered(a_run().messages)

    assert "[request tool-return, rejected by the schema] name must be a string" in rendered_run
    assert "[request tool-return, refused by the tool] no city named Atlantis" in rendered_run
    assert "[request tool-return] sunny and mild" in rendered_run


def test_retry_feedback_reads_as_its_content() -> None:
    feedback = RetryFeedbackPart(content="Please return text.", retry=RetryInfo(cause="no_output"), timestamp=AT)

    assert text_of(feedback) == "Please return text."
    assert shown(feedback) == "Please return text."


def test_the_outline_says_when_a_call_was_never_answered() -> None:
    assert "called lookup_city (unanswered)" in outline(transcript(question(), call("c1")))


def test_long_outline_and_earlier_run_lists_page_with_stable_next_calls() -> None:
    given = transcript(*(answer("x" * 190) for _ in range(150)), answer("OUTLINE-END"))
    earlier = [replace(EARLIER, source=f"record:run-{index}", first_prompt="x" * 190) for index in range(150)]

    first_outline = outline(given)
    first_runs = runs_summary(earlier)

    assert len(first_outline) < 20200
    assert "Continue with outline(run='test:run', offset=20000)" in first_outline
    assert "OUTLINE-END" in outline(given, offset=20000)
    assert len(first_runs) < 20200
    assert "Continue with earlier_runs(offset=20000)" in first_runs
    assert "record:run-149" in runs_summary(earlier, offset=40000)


def test_the_outline_names_how_each_call_ended() -> None:
    given = transcript(
        question(),
        call("c1"),
        result("c1", "ok", outcome="success"),
        call("c2"),
        result("c2", "boom", outcome="failed"),
        call("c3"),
        result("c3", "no", outcome="denied"),
        call("c4"),
    )

    line = outline(given).splitlines()[0]

    assert "called lookup_city (answered)" in line
    assert "called lookup_city (raised)" in line
    assert "called lookup_city (denied)" in line
    assert "called lookup_city (unanswered)" in line


def test_read_prefixes_every_part_with_its_span_and_answers_when_the_window_is_empty() -> None:
    body = read(a_run(), 0, 1, 3)

    assert body.splitlines() == ["0.1.0 [tool-call] {'name': 'Paris'}", "0.2.0 [tool-return] sunny and mild"]
    assert read(a_run(), 0, 9, 9) == "step 0 holds no message in [9, 9)"


def test_read_refuses_a_step_the_run_does_not_have() -> None:
    given = a_run()

    with pytest.raises(ValueError, match="no step 99 in test:run: it has 1 steps"):
        read(given, 99, 0, 3)
    with pytest.raises(ValueError, match="no step -1 in test:run: it has 1 steps"):
        read(given, -1, 0, 3)


def test_find_answers_the_spans_that_hold_the_text_case_insensitively() -> None:
    assert find(a_run(), "SUNNY AND").startswith("0.2.0 sunny and mild | read(step=0, start=2, end=3,")
    assert find(a_run(), "nothing here") == "nothing holds 'nothing here'"


def test_find_stops_at_fifty_matches() -> None:
    pairs = [message for index in range(FIND_LIMIT + 5) for message in (call(f"c{index}"), result(f"c{index}", "SHARED-TEXT"))]
    given = transcript(*pairs)

    first = find(given, "SHARED-TEXT")
    assert sum("SHARED-TEXT" in line and "Continue" not in line for line in first.splitlines()) == FIND_LIMIT
    assert "offset=50" in first
    assert len(find(given, "SHARED-TEXT", offset=50).splitlines()) == 5


def test_read_pages_all_recorded_instructions_and_complete_tool_definitions() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(ModelRequest(parts=[UserPromptPart(content="hello")], instructions="x" * 2100 + "INSTRUCTION-END"),),
                ordinals=(0,),
                parameters=ModelRequestParameters(
                    function_tools=[
                        ToolDefinition(
                            name="lookup",
                            description="LOOKUP-DESCRIPTION",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"key": {"type": "string", "description": "SCHEMA-MARKER"}},
                            },
                        )
                    ]
                ),
            ),
        ),
    )

    first = read(given, 0, resource="instructions")
    assert "INSTRUCTION-END" not in first
    assert "offset=2000" in first
    assert "INSTRUCTION-END" in read(given, 0, resource="instructions", offset=2000)
    schemas = read(given, 0, resource="tools")
    assert "LOOKUP-DESCRIPTION" in schemas
    assert "SCHEMA-MARKER" in schemas
    assert "parameters_json_schema" in schemas
    assert "SCHEMA-MARKER" in find(given, "schema-marker")
    assert "resource='tools'" in find(given, "schema-marker")
    assert "INSTRUCTION-END" in find(given, "instruction-end")


def test_output_tool_schemas_are_evidence_even_without_function_tools() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(question(),),
                ordinals=(0,),
                parameters=ModelRequestParameters(
                    output_mode="tool",
                    output_tools=[
                        ToolDefinition(
                            name="final_result",
                            description="OUTPUT_SCHEMA_ONLY_MARKER",
                            parameters_json_schema={"type": "object", "properties": {"verdict": {"enum": ["accept", "reject"]}}},
                        )
                    ],
                ),
            ),
        ),
    )

    body = read(given, 0, resource="tools")

    assert "OUTPUT_SCHEMA_ONLY_MARKER" in body
    assert '"enum": [' in body
    assert '"accept"' in body
    assert "OUTPUT_SCHEMA_ONLY_MARKER" in find(given, "OUTPUT_SCHEMA_ONLY_MARKER")
    assert "resource='tools'" in find(given, "OUTPUT_SCHEMA_ONLY_MARKER")


def test_native_output_schema_and_native_tool_configuration_are_readable_and_searchable() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(question(),),
                ordinals=(0,),
                parameters=ModelRequestParameters(
                    output_mode="native",
                    output_object=OutputObjectDefinition(
                        name="Decision",
                        description="OUTPUT_OBJECT_MARKER",
                        strict=True,
                        json_schema={"type": "object", "properties": {"decision": {"const": "OUTPUT_VALUE_MARKER"}}},
                    ),
                    native_tools=[WebSearchTool(allowed_domains=["native-marker.example"], max_uses=4)],
                ),
            ),
        ),
    )

    body = read(given, 0, resource="tools")

    for marker in ("OUTPUT_OBJECT_MARKER", "OUTPUT_VALUE_MARKER", "native-marker.example"):
        assert marker in body
        assert marker in find(given, marker)
    assert '"strict": true' in body
    assert '"max_uses": 4' in body


def test_search_read_offset_reaches_a_late_output_tool_after_other_recorded_metadata() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(question(),),
                ordinals=(0,),
                parameters=ModelRequestParameters(
                    function_tools=[ToolDefinition(name="lookup", description="f" * 3000)],
                    native_tools=[WebSearchTool(allowed_domains=["example.com"])],
                    output_mode="tool",
                    output_tools=[ToolDefinition(name="final_result", description="o" * 3000 + "LATE_OUTPUT_SCHEMA_MARKER")],
                ),
            ),
        ),
    )

    hit = find(given, "LATE_OUTPUT_SCHEMA_MARKER")
    offset = re.search(r"offset=(\d+)", hit)

    assert offset is not None
    assert int(offset[1]) > 6000
    assert "LATE_OUTPUT_SCHEMA_MARKER" in read(given, 0, resource="tools", offset=int(offset[1]))


def test_read_bounds_the_whole_result_without_losing_a_late_part() -> None:
    given = transcript(answer("a" * 2500 + "LATE-PART"), answer("b" * 2500 + "LAST-PART"))

    pages = [read(given, 0, 0, 2, offset=offset) for offset in (0, 2000, 4000)]

    assert all(len(page) < 2250 for page in pages)
    assert "LATE-PART" in pages[1]
    assert "LAST-PART" in pages[2]
    assert "LATE-PART" in find(given, "LATE-PART")
    assert "offset=" in find(given, "LATE-PART")


def test_missing_recorded_tools_and_invalid_offsets_are_not_empty_evidence() -> None:
    assert "not recorded" in read(a_run(), 0, resource="tools")
    with pytest.raises(ValueError, match="offset"):
        read(a_run(), 0, offset=-1)
    with pytest.raises(ValueError, match="offset"):
        find(a_run(), "mild", offset=-1)


def test_the_initial_lead_view_bounds_large_evidence_and_teaches_its_continuation() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(
                    ModelRequest(parts=[UserPromptPart(content="q" * 30000)], instructions="i" * 30000),
                    call("c", args={"large": "v" * 30000}),
                ),
                ordinals=(0, 1),
                parameters=ModelRequestParameters(function_tools=[ToolDefinition(name="lookup_city", description="t" * 30000)]),
            ),
        ),
    )
    lead = Lead(source=given.source, symptom="duplicate_call", spans=(Span(step=0, message=1, part=0),), found_by="duplicate_call")

    body = view_for(lead, given)

    assert len(body) < 12000
    assert body.count("Continue with read(") >= 4


def test_a_span_reads_step_message_part_and_a_bad_one_is_refused() -> None:
    given = a_run()

    assert parse_span("0.2.0", given) == Span(step=0, message=2, part=0)
    with pytest.raises(ValueError, match=r"step\.message\.part"):
        parse_span("0.2", given)
    with pytest.raises(ValueError, match="not in test:run"):
        parse_span("0.99.0", given)


def test_the_moment_view_carries_the_symptom_its_candidates_the_tools_and_what_the_agent_was_told() -> None:
    told = ModelRequest(
        parts=[SystemPromptPart(content="SYSTEM-MARKER", timestamp=AT), UserPromptPart(content="q", timestamp=AT)],
        instructions="INSTRUCTIONS-MARKER",
    )
    given = transcript(told, call("c1"), result("c1", "boom", outcome="failed"), call("c2"), result("c2", "boom", outcome="failed"))
    lead = Lead(source="test:run", symptom="retried_after_vague_error", spans=(Span(step=0, message=2, part=0),), found_by="inspector")

    view = view_for(lead, given)

    assert "retried_after_vague_error" in view
    assert "tool_error_message" in view
    assert "SYSTEM-MARKER" in view
    assert "INSTRUCTIONS-MARKER" in view
    assert "A1" in view
    assert "A6" in view
    assert "The run, one line per step:" not in view


def test_the_view_carries_what_the_agent_was_told_when_the_window_is_a_tail() -> None:
    told = ModelRequest(
        parts=[SystemPromptPart(content="SYSTEM-MARKER", timestamp=AT), UserPromptPart(content="q", timestamp=AT)],
        instructions="INSTRUCTIONS-MARKER",
    )
    pairs = [message for index in range(8) for message in (call(f"c{index}"), result(f"c{index}"))]
    given = transcript(told, *pairs)
    long_ago = Lead(source="test:run", symptom="duplicate_call", spans=(Span(step=0, message=15, part=0),), found_by="duplicate_call")

    assert len(given.seen_at(long_ago.spans[-1])) > WINDOW

    view = view_for(long_ago, given)

    assert "SYSTEM-MARKER" in view
    assert "INSTRUCTIONS-MARKER" in view


def test_a_run_horizon_adds_the_outline_and_a_conversation_horizon_adds_the_earlier_runs() -> None:
    given = a_run()
    of_the_run = Lead(source="test:run", symptom="duplicate_call", spans=(Span(step=0, message=1, part=0),), found_by="duplicate_call")
    of_the_conversation = Lead(
        source="test:run", symptom="call_for_known_fact", spans=(Span(step=0, message=1, part=0),), found_by="inspector"
    )

    run_view = view_for(of_the_run, given)
    conversation_view = view_for(of_the_conversation, given, earlier=[EARLIER])

    assert "The run, one line per step:" in run_view
    assert "Earlier runs of this conversation, newest first:" not in run_view
    assert "The run, one line per step:" in conversation_view
    assert "record:run-0" in conversation_view
    assert "index the notes" in conversation_view


def test_no_earlier_run_says_so_rather_than_leaving_a_blank() -> None:
    assert runs_summary(()) == "(none)"
    assert runs_summary([EARLIER]).startswith("- record:run-0 | agent harness-author | started 2026-09-09T09:00:00+00:00 |")


def test_a_refusal_is_appended_to_the_view() -> None:
    given = with_refusal("the view", "tool_schema cannot be behind duplicate_call")

    assert given.startswith("the view")
    assert "Your previous answer was refused: tool_schema cannot be behind duplicate_call" in given


LABEL_CASES: list[tuple[ModelRequestPart | ModelResponsePart, str]] = [
    (UserPromptPart(content="hi", timestamp=AT), "step 2, the user's prompt"),
    (SystemPromptPart(content="be nice", timestamp=AT), "step 2, the system prompt"),
    (TextPart(content="ok"), "step 2, the model said"),
    (ThinkingPart(content="hmm"), "step 2, the model's thinking"),
    (ToolCallPart(tool_name="lookup_city", args={}, tool_call_id="c1"), "step 2, the model's call to lookup_city"),
    (ToolReturnPart(tool_name="lookup_city", content="ok", tool_call_id="c1"), "step 2, the return of lookup_city"),
    (
        ToolReturnPart(tool_name="lookup_city", content="bad args", tool_call_id="c1", outcome="retried"),
        "step 2, the retry asked of lookup_city",
    ),
    (RetryFeedbackPart(content="bad output", retry=RetryInfo(cause="validation_error")), "step 2, the retry feedback"),
    (CompactionPart(), "step 2, compaction"),
]


@pytest.mark.parametrize(("part", "expected"), LABEL_CASES)
def test_label_of_names_the_step_and_who_wrote_the_part(part: ModelRequestPart | ModelResponsePart, expected: str) -> None:
    assert label_of(Span(step=2, message=0, part=0), part) == expected


def test_shown_unfolds_json_held_in_a_string_and_renders_a_tree() -> None:
    part = ToolReturnPart(
        tool_name="fetch_workflow_spec",
        content={
            "output": '{"summary": "research-digest v13", "fit": "STALE: re-check\\nFITS THIS RUN", '
            '"fits_here": true, "spec_yaml": "name: research-digest\\nversion: 13"}'
        },
        tool_call_id="c",
    )

    lines = shown(part).splitlines()

    for expected in (
        "output:",
        "  summary: research-digest v13",
        "  fit:",
        "    STALE: re-check",
        "    FITS THIS RUN",
        "  fits_here: true",
        "  spec_yaml:",
        "    name: research-digest",
        "    version: 13",
    ):
        assert expected in lines
    rendered = shown(part)
    assert "\\n" not in rendered
    assert "{'" not in rendered


def test_shown_keeps_a_plain_string() -> None:
    part = ToolReturnPart(tool_name="write_note", content="the page already exists", tool_call_id="c1")

    assert shown(part) == "the page already exists"


def test_shown_renders_a_list_of_dicts() -> None:
    part = ToolReturnPart(tool_name="list_notes", content=[{"name": "a.md", "size": 10}, {"name": "b.md", "size": 20}], tool_call_id="c1")

    assert shown(part) == "-\n  name: a.md\n  size: 10\n-\n  name: b.md\n  size: 20"


def test_shown_renders_call_arguments_as_a_tree() -> None:
    part = ToolCallPart(tool_name="write_note", args={"path": "a.md", "count": 3}, tool_call_id="c1")

    assert shown(part) == "path: a.md\ncount: 3"


def test_shown_cuts_at_the_limit_and_says_how_much_is_left() -> None:
    part = ToolReturnPart(tool_name="dump", content="x" * 50, tool_call_id="c1")

    assert shown(part, limit=10) == f"{'x' * 10}\n... 40 more characters"


def test_shown_renders_each_item_of_a_users_sequence_content_on_its_own_line() -> None:
    part = UserPromptPart(content=["index this page", ImageUrl(url="https://example.com/a.png")], timestamp=AT)

    assert shown(part) == "index this page\n[ImageUrl]"


def _step_transcript(*messages, source: str = "test:run") -> Transcript:
    return Transcript(source=source, steps=(Step(messages=messages, ordinals=tuple(range(len(messages)))),))


def test_turn_at_starts_at_the_user_prompt_two_messages_before_the_cited_one() -> None:
    given = _step_transcript(question(), call("c1"), result("c1"), question("second ask"), call("c2"), result("c2"))
    spans = [Span(step=0, message=5, part=0)]

    turn = turn_at(given, 0, spans)

    assert turn.step == 0
    assert turn.first == 3
    assert len(turn.messages) == 3
    first_message = turn.messages[0]
    assert isinstance(first_message, ModelRequest)
    assert isinstance(first_message.parts[0], UserPromptPart)
    assert first_message.parts[0].content == "second ask"
    assert turn.cited == (2,)


def test_turn_at_a_cited_first_message_starts_at_index_zero() -> None:
    given = _step_transcript(question(), call("c1"), result("c1"))
    spans = [Span(step=0, message=0, part=0)]

    turn = turn_at(given, 0, spans)

    assert turn.step == 0
    assert turn.first == 0
    assert len(turn.messages) == 3
    assert turn.cited == (0,)


def test_turn_at_appends_the_steps_response_only_when_a_next_step_holds_it() -> None:
    history = (question(), call("c1"), result("c1"))
    with_next = Transcript(
        source="test:run",
        steps=(Step(messages=history, ordinals=(0, 1, 2)), Step(messages=(*history, answer("done")), ordinals=(0, 1, 2, 3))),
    )
    last_step_only = Transcript(source="test:run", steps=(Step(messages=history, ordinals=(0, 1, 2)),))
    spans = [Span(step=0, message=2, part=0)]

    with_response = turn_at(with_next, 0, spans)
    without_response = turn_at(last_step_only, 0, spans)

    assert len(with_response.messages) == 4
    last = with_response.messages[-1]
    assert isinstance(last, ModelResponse)
    said = last.parts[0]
    assert isinstance(said, TextPart)
    assert said.content == "done"
    assert len(without_response.messages) == 3


def test_turns_for_two_spans_on_one_step_give_one_turn_with_both_cited() -> None:
    given = _step_transcript(question(), call("c1"), result("c1"))
    spans = [Span(step=0, message=1, part=0), Span(step=0, message=2, part=0)]

    turns = turns_for(given, spans)

    assert len(turns) == 1
    assert turns[0].step == 0
    assert turns[0].cited == (1, 2)


def test_turns_for_spans_on_two_steps_give_two_turns_ascending() -> None:
    history = (question(), call("c1"), result("c1"))
    given = Transcript(
        source="test:run",
        steps=(Step(messages=history, ordinals=(0, 1, 2)), Step(messages=(*history, answer("done")), ordinals=(0, 1, 2, 3))),
    )
    spans = [Span(step=1, message=3, part=0), Span(step=0, message=1, part=0)]

    turns = turns_for(given, spans)

    assert [turn.step for turn in turns] == [0, 1]


def test_plain_whys_drops_the_coordinates() -> None:
    why = "The fetch at 5.8.0 returned STALE, but 5.9.2 fired anyway (3.6.0 empty)."

    cleaned = plain_whys([why])

    assert "5.8.0" not in cleaned
    assert "5.9.2" not in cleaned
    assert "3.6.0" not in cleaned
    assert "  " not in cleaned
    assert "()" not in cleaned
    assert cleaned == "The fetch returned STALE, but fired anyway (empty)."


def test_plain_whys_drops_a_parenthetical_that_held_only_the_coordinate() -> None:
    assert plain_whys(["nothing says to stop (0.1.0)"]) == "nothing says to stop"


def test_plain_whys_drops_a_run_of_coordinates_with_its_preposition() -> None:
    assert plain_whys(["sitting in view at 9.4.0/9.4.1, no tool failure"]) == "sitting in view, no tool failure"
    assert plain_whys(["yet in 5.9.0/5.9.2 the agent echoed it"]) == "yet the agent echoed it"
    assert plain_whys(["seen at 1.3.0 and 9.19.1 twice"]) == "seen twice"


def test_plain_whys_joins_every_why_with_one_space() -> None:
    assert plain_whys(["it retried at 0.1.0", "and got the same error"]) == "it retried and got the same error"


def _two_steps() -> Transcript:
    """Step 0 holds the prompt, a call and its return. Step 1 adds the model's answer."""
    history = (question(), call("c1"), result("c1", "sunny and mild"))
    return Transcript(
        source="test:run",
        steps=(Step(messages=history, ordinals=(0, 1, 2)), Step(messages=(*history, answer("done")), ordinals=(0, 1, 2, 3))),
    )


def test_outline_steps_count_the_messages_each_step_added_and_say_what_happened_there() -> None:
    lines = outline_steps(_two_steps())

    assert lines == [
        StepOutline(step=0, messages=3, pieces=("asked 'What is the weather in Paris?'", "called lookup_city (answered)")),
        StepOutline(step=1, messages=1, pieces=("said 'done'",)),
    ]


def test_the_outline_text_is_rendered_from_the_structured_lines() -> None:
    given = _two_steps()

    assert outline(given) == "\n".join(f"step {line.step}: {', '.join(line.pieces)}" for line in outline_steps(given))


def test_a_step_that_added_nothing_reads_nothing_new_in_the_outline_and_counts_zero() -> None:
    history = (question(), call("c1"), result("c1"))
    given = Transcript(source="test:run", steps=(Step(messages=history, ordinals=(0, 1, 2)), Step(messages=history, ordinals=(0, 1, 2))))

    assert outline_steps(given)[1] == StepOutline(step=1, messages=0, pieces=())
    assert outline(given).splitlines()[1] == "step 1: (nothing new)"


def test_step_at_answers_the_messages_the_step_added_and_where_they_start() -> None:
    given = _two_steps()

    first = step_at(given, 0)
    second = step_at(given, 1)

    assert (first.step, first.first, first.cited, len(first.messages)) == (0, 0, (), 3)
    assert (second.step, second.first, second.cited, len(second.messages)) == (1, 3, (), 1)
    said = second.messages[0]
    assert isinstance(said, ModelResponse)
    assert isinstance(said.parts[0], TextPart)
    assert said.parts[0].content == "done"


def test_step_at_refuses_a_step_the_run_does_not_have() -> None:
    with pytest.raises(ValueError, match="no step 2 in test:run: it has 2 steps"):
        step_at(_two_steps(), 2)
