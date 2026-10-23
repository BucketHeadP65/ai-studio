"""The scan lane: the inspector reads a whole run, names what it sees, and each name becomes a lead and a finding."""

from __future__ import annotations

from uuid import UUID

import pytest

from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionDef, FunctionModel

from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.watches import Watch
from core.ai.inspection.inspector import InspectorDeps, TierAgent
from core.ai.inspection.lanes import ScanRun
from core.ai.inspection.scan_run import ScanRunHandler, ScanUndecided, scan, scan_agent
from core.ai.transcripts.contracts import RunEmptyError, SchemeUnknownError, Span, Transcript, TranscriptLoaders
from core.cqrs.contracts import JobContext
from tests.support.fakes.inspection import FakeLeadsAndFindings, FakeRunsOfConversation, Opens, answer, call, question, result, transcript
from tests.unit.inspection.test_inspect_run import A_TAXONOMY, Unparseable

CONTEXT = JobContext(job_id=1, kind="scan_run", key="scan:every-run:record:run-1", attempt=1)
A_RUN = transcript(question(), call("c1"), result("c1", "ok"), call("c2"), result("c2", "ok"), answer("done"), source="record:run-1")
WATCHES = {"every-run": Watch(name="every-run", rules=("duplicate_call",))}
A_CONVERSATION = UUID("22222222-2222-4222-8222-222222222222")


def saw(symptom: str, spans: list[str], root_cause: str, surface_kind: str) -> dict[str, object]:
    return {
        "items": [
            {
                "symptom": symptom,
                "spans": spans,
                "blames": [
                    {"root_cause": root_cause, "surface_kind": surface_kind, "surface_name": "", "why": "the context already held it"}
                ],
                "explanation": "The agent already had the fact. It called the tool again anyway. Reuse what it saw.",
            }
        ]
    }


class Answers:
    """A model function that answers one scripted output tool call, and counts how often it was asked."""

    def __init__(self, output: str, args: dict[str, object]) -> None:
        self.output = output
        self.args = args
        self.calls = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        self.calls += 1
        tool = next(tool for tool in info.output_tools if tool.name.endswith(self.output))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=self.args)])


def deps_for(given: Transcript) -> InspectorDeps:
    return InspectorDeps(
        transcript=given, loaders=TranscriptLoaders(by_key={"record": Opens(**{"run-1": given})}), conversation=FakeRunsOfConversation()
    )


def a_handler(store: FakeLeadsAndFindings, function: FunctionDef, *, loaders: TranscriptLoaders | None = None) -> ScanRunHandler:
    return ScanRunHandler(
        store=store,
        loaders=loaders or TranscriptLoaders(by_key={"record": Opens(**{"run-1": A_RUN})}),
        agents=(TierAgent(tier="helper", agent=scan_agent(FunctionModel(function, model_name="scanner"))),),
        conversation=FakeRunsOfConversation(),
        questions=store,
        watches=WATCHES,
    )


async def test_invalid_scan_spans_are_recorded_as_refused_outputs() -> None:
    store = FakeLeadsAndFindings()
    handler = a_handler(store, Answers("Seen", saw("call_for_known_fact", ["90.1.0"], "agent_reasoning", "model_tier")))

    with capture_run_messages() as messages, pytest.raises(RuntimeError, match="every tier"):
        await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    parts = [part for message in messages for part in message.parts]
    assert len([part for part in parts if isinstance(part, ToolCallPart) and part.tool_name.startswith("final_result")]) == 2
    returns = [part for part in parts if isinstance(part, ToolReturnPart) and part.tool_name.startswith("final_result")]
    assert [part.outcome for part in returns] == ["retried"]


async def test_a_scan_cannot_insert_a_finding_for_an_already_dismissed_lead() -> None:
    store = FakeLeadsAndFindings()
    lead = Lead(source="record:run-1", symptom="call_for_known_fact", spans=(Span(step=0, message=1, part=0),), found_by="inspector")
    (lead_id,) = await store.add_leads("every-run", [lead])
    await store.resolve_lead(lead_id, "dismissed", "The fresh lookup was required.")
    handler = a_handler(store, Answers("Seen", saw("call_for_known_fact", ["0.1.0"], "agent_reasoning", "model_tier")))

    line = await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert store.findings == []
    assert "0 findings" in line
    assert "already settled" in line
    assert store.questions[0].answer == line


async def test_a_named_symptom_becomes_a_lead_and_the_finding_under_it() -> None:
    seen = await scan(
        scan_agent(FunctionModel(Answers("Seen", saw("call_for_known_fact", ["0.1.0"], "agent_reasoning", "model_tier")), model_name="s")),
        deps=deps_for(A_RUN),
        conversation_id=A_CONVERSATION,
    )

    assert not isinstance(seen, ScanUndecided)
    ((lead, finding),) = seen
    assert (lead.source, lead.symptom, lead.found_by) == ("record:run-1", "call_for_known_fact", "inspector")
    assert [(span.step, span.message, span.part) for span in lead.spans] == [(0, 1, 0)]
    assert finding.found_by == "inspector"
    assert finding.blames[0].what.name == "agent_reasoning"
    assert finding.explanation == "The agent already had the fact. It called the tool again anyway. Reuse what it saw."


async def test_scan_output_validation_uses_the_callers_custom_taxonomy() -> None:
    agent = scan_agent(FunctionModel(Answers("Seen", saw("improvised_page", ["0.1.0"], "hand_written_page", "tool"))))

    seen = await scan(agent, deps=deps_for(A_RUN), conversation_id=A_CONVERSATION, taxonomy=A_TAXONOMY)

    assert not isinstance(seen, ScanUndecided)
    assert seen[0][1].symptom.name == "improvised_page"


async def test_nothing_seen_is_an_empty_answer_not_a_failure() -> None:
    given = scan_agent(FunctionModel(Answers("NothingSeen", {"reason": "the run is clean"}), model_name="s"))

    assert await scan(given, deps=deps_for(A_RUN), conversation_id=A_CONVERSATION) == []


async def test_cannot_assess_scan_is_distinct_from_a_clean_run_and_preserves_its_reason() -> None:
    def uncertain(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert any(tool.name.endswith("ScanUndecided") for tool in info.output_tools)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("ScanUndecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "The required instructions were not recorded."})])

    store = FakeLeadsAndFindings()
    line = await a_handler(store, uncertain)(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert "scan undecided" in line
    assert "instructions were not recorded" in line
    assert "scanned, 0 seen" not in line
    assert store.questions[0].answer == "undecided: The required instructions were not recorded."


async def test_scan_refuses_a_symptom_cited_on_the_wrong_kind_of_part() -> None:
    agent = scan_agent(FunctionModel(Answers("Seen", saw("call_for_known_fact", ["0.0.0"], "agent_reasoning", "model_tier"))))

    with pytest.raises(UnexpectedModelBehavior) as refused:
        await scan(agent, deps=deps_for(A_RUN), conversation_id=A_CONVERSATION)
    assert "tool-call" in str(refused.value.__cause__)


async def test_scan_retries_once_and_raises_on_a_span_the_run_does_not_have() -> None:
    stubborn = Answers("Seen", saw("call_for_known_fact", ["0.99.0"], "agent_reasoning", "model_tier"))

    with pytest.raises(UnexpectedModelBehavior) as refused:
        await scan(scan_agent(FunctionModel(stubborn, model_name="s")), deps=deps_for(A_RUN), conversation_id=A_CONVERSATION)

    assert "is not in record:run-1" in str(refused.value.__cause__)
    assert stubborn.calls == 2


async def test_a_refused_scan_is_asked_once_more_then_fails_the_job() -> None:
    stubborn = Answers("Seen", saw("call_for_known_fact", ["0.99.0"], "agent_reasoning", "model_tier"))
    store = FakeLeadsAndFindings()
    handler = a_handler(store, stubborn)

    with pytest.raises(RuntimeError, match="every tier answered something the taxonomy refuses"):
        await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert stubborn.calls == 2


async def test_a_scan_that_gives_up_answers_its_question_with_the_give_up_line() -> None:
    stubborn = Answers("Seen", saw("call_for_known_fact", ["0.99.0"], "agent_reasoning", "model_tier"))
    store = FakeLeadsAndFindings()
    handler = a_handler(store, stubborn)

    with pytest.raises(RuntimeError, match=r"scanner: output validation exhausted: ") as failed:
        await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    [asked] = store.questions
    assert asked.answer is not None
    assert asked.answer == "output validation exhausted: span 0.99.0 is not in record:run-1"
    assert str(failed.value).endswith(f"scanner: {asked.answer}")


class RemembersThePrompt:
    """A model function that says nothing seen and keeps the first thing it was asked."""

    def __init__(self) -> None:
        self.prompt = ""

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0].parts[0]
        self.prompt = first.content if isinstance(first, UserPromptPart) and isinstance(first.content, str) else ""
        tool = next(tool for tool in info.output_tools if tool.name.endswith("NothingSeen"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "the run is clean"})])


async def test_the_scan_prompt_names_where_a_fix_for_each_candidate_lands() -> None:
    watcher = RemembersThePrompt()

    await scan(scan_agent(FunctionModel(watcher, model_name="w")), deps=deps_for(A_RUN), conversation_id=A_CONVERSATION)

    assert "Fix lands on" in watcher.prompt


async def test_the_handler_stores_every_lead_and_finding_and_says_what_that_came_to() -> None:
    store = FakeLeadsAndFindings()
    handler = a_handler(store, Answers("Seen", saw("call_for_known_fact", ["0.1.0"], "agent_reasoning", "model_tier")))

    line = await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert line == "record:run-1: scanned, 1 seen, 1 findings"
    assert [(row.symptom, row.watch, row.found_by) for row in store.leads] == [("call_for_known_fact", "every-run", "inspector")]
    assert [(row.found_by, row.model) for row in store.findings] == [("inspector", "scanner")]


async def test_a_clean_run_is_recorded_as_scanned_with_nothing() -> None:
    store = FakeLeadsAndFindings()
    handler = a_handler(store, Answers("NothingSeen", {"reason": "the run is clean"}))

    assert await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT) == "record:run-1: scanned, 0 seen, 0 findings"
    assert store.leads == []


async def test_a_gone_run_and_an_empty_run_both_answer_without_raising() -> None:
    class RaisesRunEmptyError:
        async def __call__(self, locator: str) -> Transcript:
            msg = f"run {locator!r} recorded no step"
            raise RunEmptyError(msg)

    store = FakeLeadsAndFindings()
    named = Answers("NothingSeen", {"reason": "the run is clean"})
    gone = a_handler(store, named, loaders=TranscriptLoaders(by_key={"record": Opens()}))
    empty = a_handler(store, named, loaders=TranscriptLoaders(by_key={"record": RaisesRunEmptyError()}))

    assert await gone(ScanRun(source="record:run-1", watch="every-run"), CONTEXT) == "record:run-1: run gone"
    assert await empty(ScanRun(source="record:run-1", watch="every-run"), CONTEXT) == "record:run-1: no steps"


@pytest.mark.parametrize(
    ("source", "error"),
    [("other:run-1", SchemeUnknownError), ("run-1", ValueError), ("broken:run-1", ValueError)],
    ids=["a scheme no loader is registered for", "a source with no scheme", "a transcript that does not parse"],
)
async def test_a_transcript_that_is_neither_gone_nor_empty_fails_the_job(source: str, error: type[Exception]) -> None:
    store = FakeLeadsAndFindings()
    named = Answers("NothingSeen", {"reason": "the run is clean"})
    loaders = TranscriptLoaders(by_key={"record": Opens(**{"run-1": A_RUN}), "broken": Unparseable()})

    with pytest.raises(error):
        await a_handler(store, named, loaders=loaders)(ScanRun(source=source, watch="every-run"), CONTEXT)

    assert named.calls == 0
    assert store.leads == []


async def test_a_tier_the_taxonomy_refuses_twice_fails_the_job() -> None:
    store = FakeLeadsAndFindings()
    handler = a_handler(store, Answers("Seen", saw("call_for_known_fact", ["0.1.0"], "tool_schema", "tool")))

    with pytest.raises(RuntimeError, match="every tier answered something the taxonomy refuses"):
        await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert store.leads == []


async def test_a_lead_the_rules_already_stored_dedupes_the_scans_own_finding() -> None:
    store = FakeLeadsAndFindings()
    await store.add_leads(
        "every-run",
        [Lead(source="record:run-1", symptom="call_for_known_fact", spans=(Span(step=0, message=1, part=0),), found_by="duplicate_call")],
    )
    item = {
        "symptom": "call_for_known_fact",
        "spans": ["0.1.0"],
        "blames": [{"root_cause": "agent_reasoning", "surface_kind": "model_tier", "surface_name": "", "why": "it repeats the mistake"}],
        "explanation": "The agent already knew the fact. It called the tool again anyway. Reuse what it already knows.",
    }
    handler = a_handler(store, Answers("Seen", {"items": [item, item]}))

    line = await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert line == "record:run-1: scanned, 2 seen, 1 findings"
    assert len(store.leads) == 1
    assert len(store.findings) == 1


class RemembersEveryConversation:
    """A model function that answers what it was scripted with and keeps the conversation id of every request."""

    def __init__(self, output: str, args: dict[str, object]) -> None:
        self.output = output
        self.args = args
        self.conversations: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.conversations.append(messages[0].conversation_id)
        tool = next(tool for tool in info.output_tools if tool.name.endswith(self.output))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=self.args)])


async def test_the_retry_after_a_refusal_stays_in_the_question_s_own_conversation() -> None:
    store = FakeLeadsAndFindings()
    stubborn = RemembersEveryConversation("Seen", saw("call_for_known_fact", ["0.99.0"], "agent_reasoning", "model_tier"))
    handler = a_handler(store, stubborn)

    with pytest.raises(RuntimeError, match="every tier answered something the taxonomy refuses"):
        await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    assert len(stubborn.conversations) == 2
    assert stubborn.conversations[0] == stubborn.conversations[1]
    assert stubborn.conversations[0] is not None


async def test_a_scan_records_its_question_with_no_lead_and_its_findings_point_at_it() -> None:
    store = FakeLeadsAndFindings()
    handler = a_handler(store, Answers("Seen", saw("call_for_known_fact", ["0.1.0"], "agent_reasoning", "model_tier")))

    line = await handler(ScanRun(source="record:run-1", watch="every-run"), CONTEXT)

    (asked,) = store.questions
    assert (asked.kind, asked.tier, asked.model, asked.lead_id, asked.watch) == ("scan", "helper", "scanner", None, "every-run")
    assert asked.answer == line
    assert [row.question_id for row in store.findings] == [asked.question_id]
