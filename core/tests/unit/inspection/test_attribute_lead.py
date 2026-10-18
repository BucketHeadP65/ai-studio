"""The inspector's lane: one lead in, a finding stored or a refusal recorded, cheap tier first."""

import asyncio

import pytest

from pydantic import JsonValue
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from core.ai.inspection.attribute_lead import AttributeLeadHandler
from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.inspector import Attribution, InspectorDeps, NoIssue, TierAgent, Undecided, inspector_agent, model_name
from core.ai.inspection.lanes import AttributeLead
from core.ai.transcripts.contracts import RunEmptyError, Span, Transcript, TranscriptLoaders
from core.cqrs.contracts import JobContext
from tests.support.fakes.inspection import FakeLeadsAndFindings, FakeRunsOfConversation, Opens, answer, call, question, result, transcript
from tests.support.fakes.inspection_verdicts import HeldUndecided, HeldVerdict

CONTEXT = JobContext(job_id=1, kind="attribute_lead", key="1", attempt=1)
A_TRANSCRIPT = transcript(question(), call("c1"), result("c1", "ok"), call("c2"), result("c2", "ok"), answer("done"), source="record:run-1")
A_LEAD = Lead(
    source="record:run-1",
    symptom="duplicate_call",
    spans=(Span(step=0, message=1, part=0), Span(step=0, message=3, part=0)),
    found_by="duplicate_call",
)
DECIDED = {
    "symptom": "duplicate_call",
    "blames": [{"root_cause": "agent_reasoning", "surface_kind": "model_tier", "surface_name": "", "why": "it asked twice"}],
    "explanation": "The agent already had the lookup's result. It called the same tool again anyway. Reuse the earlier result.",
}


def undecided_model() -> FunctionModel:
    def answer_undecided(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "cannot tell"})])

    return FunctionModel(answer_undecided, model_name="undecided-model")


async def a_handler(store: FakeLeadsAndFindings, *agents: Agent[InspectorDeps, Attribution | Undecided | NoIssue]) -> AttributeLeadHandler:
    loaders = TranscriptLoaders(by_key={"record": Opens(**{"run-1": A_TRANSCRIPT})})
    ladder = tuple(TierAgent(tier=tier, agent=agent) for tier, agent in zip(("helper", "worker"), agents, strict=False))
    return AttributeLeadHandler(store=store, loaders=loaders, agents=ladder, conversation=FakeRunsOfConversation(), questions=store)


async def test_the_first_tier_that_decides_stores_the_finding_under_the_lead() -> None:
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(
        store,
        inspector_agent(TestModel(custom_output_args=DECIDED, model_name="first-tier")),
        inspector_agent(TestModel(custom_output_args=DECIDED, model_name="second-tier")),
    )

    answer_line = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    (finding,) = store.findings
    assert finding.lead_id == lead_id
    assert finding.found_by == "inspector"
    assert finding.model == "first-tier"
    assert answer_line == f"lead {lead_id}: finding {finding.finding_id} by first-tier"


async def test_every_tier_undecided_records_the_refusal_and_keeps_the_lead() -> None:
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(undecided_model()))

    answer_line = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert store.findings == []
    assert store.refusals == {lead_id: "undecided-model: undecided: cannot tell"}
    assert answer_line == f"lead {lead_id}: undecided (undecided-model: undecided: cannot tell)"
    assert store.leads[0].resolution == "undecided"
    assert await store.settled([lead_id]) == set()


async def test_explicit_no_issue_dismisses_a_lead_and_is_not_asked_again() -> None:
    calls = []

    def justified(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(messages)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("NoIssue"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "The user asked for a fresh read."})])

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(FunctionModel(justified)))

    line = await handler(AttributeLead(lead_id=lead_id), CONTEXT)
    again = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert "dismissed" in line
    assert "settled" in again
    assert store.findings == []
    assert store.leads[0].resolution == "dismissed"
    assert store.leads[0].refusal == "The user asked for a fresh read."
    assert len(calls) == 1
    assert await store.settled([lead_id]) == {lead_id}


async def test_exhausted_structured_output_retries_record_invalid_and_answer_the_question() -> None:
    def malformed(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        tool = next(item for item in info.output_tools if item.name.endswith("Attribution"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={})])

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(FunctionModel(malformed)))

    result = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert store.leads[0].resolution == "invalid"
    assert store.leads[0].attempted_at is not None
    assert "invalid" in result
    assert store.questions[0].answer is not None
    assert "validation" in store.questions[0].answer
    assert await store.settled([lead_id]) == set()


async def test_taxonomy_rejections_are_recorded_and_neither_invalid_answer_is_accepted() -> None:
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(FunctionModel(_RefusesEveryTime({**DECIDED, "symptom": "not_in_taxonomy"}))))

    with capture_run_messages() as messages:
        await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    parts = [part for message in messages for part in message.parts]
    outputs = [part for part in parts if isinstance(part, ToolCallPart) and part.tool_name.startswith("final_result")]
    accepted = [part for part in parts if isinstance(part, ToolReturnPart) and part.tool_name.startswith("final_result")]
    retries = [part for part in parts if isinstance(part, RetryPromptPart)]
    assert store.leads[0].resolution == "invalid"
    assert len(outputs) == 2
    assert len(retries) == 1
    assert accepted == []


async def test_only_the_selected_output_gets_the_framework_acceptance_confirmation() -> None:
    def several_outputs(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        no_issue = next(item for item in info.output_tools if item.name.endswith("NoIssue"))
        attribution = next(item for item in info.output_tools if item.name.endswith("Attribution"))
        return ModelResponse(parts=[
            ToolCallPart(tool_name=no_issue.name, tool_call_id="selected", args={"reason": "The repeated request was justified."}),
            ToolCallPart(tool_name=attribution.name, tool_call_id="skipped", args={"invalid": "not even schema valid"}),
        ])

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(FunctionModel(several_outputs)))

    with capture_run_messages() as messages:
        await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    returns = {part.tool_call_id: part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)}
    assert returns["selected"].content == "Final result processed."
    assert returns["skipped"].content == "Output tool not used - a final result was already processed."
    assert returns["selected"].outcome == returns["skipped"].outcome == "success"
    assert store.leads[0].resolution == "dismissed"
    assert store.findings == []


@pytest.mark.parametrize("failure", [ConnectionError("network down"), ModelHTTPError(503, "gateway"), UnexpectedModelBehavior("bad provider envelope")])
async def test_provider_failures_remain_retryable_job_errors(failure: Exception) -> None:
    def failing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        raise failure

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(store, inspector_agent(FunctionModel(failing)))

    with pytest.raises(type(failure)):
        await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert store.leads[0].resolution == "open"
    assert store.leads[0].attempted_at is None


async def test_stale_uncertainty_cannot_reopen_a_concurrent_dismissal() -> None:
    held = HeldUndecided()

    def dismiss(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        tool = next(item for item in info.output_tools if item.name.endswith("NoIssue"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "The repetition was justified."})])

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    stale = await a_handler(store, inspector_agent(FunctionModel(held)))
    deciding = await a_handler(store, inspector_agent(FunctionModel(dismiss)))
    pending = asyncio.create_task(stale(AttributeLead(lead_id=lead_id), CONTEXT))
    try:
        async with asyncio.timeout(2):
            await held.entered.wait()
            await deciding(AttributeLead(lead_id=lead_id), CONTEXT)
            held.release.set()
            stale_line = await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert store.leads[0].resolution == "dismissed"
    assert store.leads[0].refusal == "The repetition was justified."
    assert await store.settled([lead_id]) == {lead_id}
    assert stale_line == f"lead {lead_id}: already settled"
    assert store.questions[0].answer == stale_line


@pytest.mark.parametrize("valid_loses", [True, False])
async def test_the_first_final_verdict_wins_and_the_loser_answers_truthfully(valid_loses: bool) -> None:
    held = HeldVerdict("Attribution", DECIDED) if valid_loses else HeldVerdict("NoIssue", {"reason": "No problem."})

    def winner(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        output = "NoIssue" if valid_loses else "Attribution"
        tool = next(item for item in info.output_tools if item.name.endswith(output))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "No problem."} if valid_loses else DECIDED)])

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    stale = await a_handler(store, inspector_agent(FunctionModel(held)))
    deciding = await a_handler(store, inspector_agent(FunctionModel(winner)))
    pending = asyncio.create_task(stale(AttributeLead(lead_id=lead_id), CONTEXT))
    try:
        async with asyncio.timeout(2):
            await held.entered.wait()
            await deciding(AttributeLead(lead_id=lead_id), CONTEXT)
            held.release.set()
            line = await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    assert line == f"lead {lead_id}: already settled"
    assert store.questions[0].answer == line
    assert len(store.findings) == (0 if valid_loses else 1)
    assert (store.leads[0].resolution == "dismissed") is valid_loses


class _RefusesEveryTime:
    """A model function that always answers a blame the taxonomy refuses, and counts how often it was asked."""

    def __init__(self, args: dict[str, JsonValue]) -> None:
        self.args = args
        self.calls = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        self.calls += 1
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Attribution"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=self.args)])


async def test_a_refused_answer_is_asked_once_more_then_passes_to_the_next_tier() -> None:
    refused = {
        "symptom": "duplicate_call",
        "blames": [{"root_cause": "tool_schema", "surface_kind": "tool", "surface_name": "lookup_city", "why": "no"}],
        "explanation": "The agent asked the tool about the same thing twice. It should have reused the first answer.",
    }
    always_refuses = _RefusesEveryTime(refused)
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(
        store,
        inspector_agent(FunctionModel(always_refuses, model_name="first-tier")),
        inspector_agent(TestModel(custom_output_args=DECIDED, model_name="second-tier")),
    )

    answer_line = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert always_refuses.calls == 2
    assert len(store.findings) == 1
    assert answer_line == f"lead {lead_id}: finding {store.findings[0].finding_id} by second-tier"


async def test_a_lead_that_is_gone_and_a_run_that_is_gone_both_answer_without_raising() -> None:
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads(
        "every-run", [Lead(source="record:gone", symptom="duplicate_call", spans=A_LEAD.spans, found_by="duplicate_call")]
    )
    handler = await a_handler(store, inspector_agent(TestModel(custom_output_args=DECIDED)))

    assert await handler(AttributeLead(lead_id=lead_id + 5), CONTEXT) == f"lead {lead_id + 5}: gone"
    assert await handler(AttributeLead(lead_id=lead_id), CONTEXT) == f"lead {lead_id}: run gone"
    assert store.refusals[lead_id] == "run gone"


async def test_a_lead_over_an_empty_run_records_the_refusal() -> None:
    class RaisesRunEmptyError:
        async def __call__(self, locator: str) -> Transcript:
            msg = f"run {locator!r} recorded no step"
            raise RunEmptyError(msg)

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = AttributeLeadHandler(
        store=store,
        loaders=TranscriptLoaders(by_key={"record": RaisesRunEmptyError()}),
        agents=(TierAgent(tier="helper", agent=inspector_agent(TestModel(custom_output_args=DECIDED))),),
        conversation=FakeRunsOfConversation(),
        questions=store,
    )

    answer_line = await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert answer_line == f"lead {lead_id}: no steps"
    assert store.refusals[lead_id] == "no steps"


async def test_a_loader_that_cannot_reach_its_store_fails_the_job() -> None:
    class Down:
        async def __call__(self, locator: str) -> Transcript:
            del locator
            msg = "db down"
            raise ConnectionError(msg)

    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = AttributeLeadHandler(
        store=store,
        loaders=TranscriptLoaders(by_key={"record": Down()}),
        agents=(TierAgent(tier="helper", agent=inspector_agent(TestModel(custom_output_args=DECIDED))),),
        conversation=FakeRunsOfConversation(),
        questions=store,
    )

    with pytest.raises(ConnectionError):
        await handler(AttributeLead(lead_id=lead_id), CONTEXT)


def test_a_models_name_is_what_the_finding_records() -> None:
    assert model_name(inspector_agent(TestModel())) == "test"
    assert model_name(inspector_agent(undecided_model())) == "undecided-model"


class _RefusesAndRemembers:
    """A model function that always answers a blame the taxonomy refuses and keeps every conversation id it saw."""

    def __init__(self, args: dict[str, JsonValue]) -> None:
        self.args = args
        self.conversations: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.conversations.append(messages[0].conversation_id)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Attribution"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=self.args)])


async def test_one_conversation_per_tier_and_the_retry_stays_inside_it() -> None:
    refused = {
        "symptom": "duplicate_call",
        "blames": [{"root_cause": "tool_schema", "surface_kind": "tool", "surface_name": "lookup_city", "why": "no"}],
        "explanation": "The agent asked the tool about the same thing twice. It should have reused the first answer.",
    }
    first, second = _RefusesAndRemembers(refused), _RefusesAndRemembers(refused)
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(
        store,
        inspector_agent(FunctionModel(first, model_name="first-tier")),
        inspector_agent(FunctionModel(second, model_name="second-tier")),
    )

    await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert len(first.conversations) == 2
    assert first.conversations[0] == first.conversations[1]
    assert len(second.conversations) == 2
    assert second.conversations[0] == second.conversations[1]
    assert first.conversations[0] != second.conversations[0]
    assert store.leads[0].resolution == "invalid"
    assert await store.settled([lead_id]) == set()


async def test_each_tier_gets_its_own_question_row_and_the_deciding_one_is_on_the_finding() -> None:
    store = FakeLeadsAndFindings()
    (lead_id,) = await store.add_leads("every-run", [A_LEAD])
    handler = await a_handler(
        store, inspector_agent(undecided_model()), inspector_agent(TestModel(custom_output_args=DECIDED, model_name="second-tier"))
    )

    await handler(AttributeLead(lead_id=lead_id), CONTEXT)

    assert [(row.kind, row.tier, row.model, row.lead_id, row.watch) for row in store.questions] == [
        ("attribute", "helper", "undecided-model", lead_id, "every-run"),
        ("attribute", "worker", "second-tier", lead_id, "every-run"),
    ]
    assert store.questions[0].answer == "undecided: cannot tell"
    assert len({row.conversation_id for row in store.questions}) == 2
    (finding,) = store.findings
    assert finding.question_id == store.questions[1].question_id
    assert store.questions[1].answer == f"lead {lead_id}: finding {finding.finding_id} by second-tier"
