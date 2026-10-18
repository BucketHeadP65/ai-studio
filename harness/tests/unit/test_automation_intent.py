"""What a person asked for, recorded beside the draft the conversation proposes.

Two halves meet here. A turn asked to design something settles a draft and records the request
that produced it, and it reaches nothing else: no run starts and no schedule is created, however
finished the graph looks. The person's own edit to that record lands through the intent route, and
the next read of the session gives it back.

The unanswered-field reading stays separate from the other two readings of the same draft, so a
panel can put "you have not said what a run does when it cannot finish" somewhere other than
"this run cannot build that step".
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, cast

import yaml

from core.ai.workflows.contracts import StepRegistry, Workflow, WorkflowContract, WorkflowIntent
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimits

from harness.features.authoring.domain import Draft
from harness.features.authoring.process import CHECK_TOOL, RUN_TOOL, STARTS_WORK, WorkflowProcess
from harness.features.authoring.service import draft_payload, record_intent
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Conversation, NoSessionLog, Runtime, SessionPool, remembered_draft
from harness.settings import RunSettings
from tests.support.authoring import a_chat, a_controller, a_turn, one_session
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import WorkflowLibrary
    from fastapi.responses import JSONResponse

ASKED_FOR = WorkflowIntent(
    request="Every Monday, tell me which of my open questions the week's new reading actually answered",
    expected_result="A short list of questions with the reading that answers each one, and nothing else.",
    trigger="scheduled",
    assumptions=("The knowledge base to read is the one this session already names",),
)
"""One statement a person made, still owing three of the four expected answers."""

EDITED = ASKED_FOR.model_copy(update={"assumptions": ("Read only the knowledge base I pick at the start of the run",)})
"""The same statement after the person rewrote the assumption that was made on their behalf."""

STILL_OPEN = ["success_example", "empty_input_behavior", "failure_behavior"]
"""What `ASKED_FOR` has not answered, in the order the intent asks for them."""


def _session(tmp_path: Path, registry: StepRegistry, *, draft: Draft | None = None) -> Runtime:
    """One session holding `registry` and `draft`, keeping its proposal in a file of its own."""
    return Runtime(
        registry=registry,
        library=cast("WorkflowLibrary", None),
        agent=cast("Agent[Runtime, str]", object()),
        conversation=Conversation(draft=draft if draft is not None else Draft()),
        draft_file=tmp_path / "draft.json",
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
    )


def _process(tmp_path: Path, session: Runtime, store: FakeSchedules) -> WorkflowProcess:
    """The workflow capability as a chat turn mounts it: the draft checker and the runner, together."""
    port = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "library"),
        vocabulary=FOLDER,
        expose=(),
        registry=session.registry,
        saved_by="test",
    )
    return WorkflowProcess(
        port=port,
        records=records_under(tmp_path),
        reporter=SpendReporter(FakeTelemetry()),
        scheduler=Scheduler(store, store, FakeBroadcaster()),
        sessions=APool(SessionPool(session)),
        topics=FakeBroadcaster(),
        run=RunSettings(),
    )


def _checks_the_draft(spec: Workflow, intent: WorkflowIntent) -> FunctionModel:
    """A model that checks one draft, records what was asked for, and says so. It calls nothing else."""

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "check_draft",
                        {
                            "spec_yaml": yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False),
                            "title": "Monday question sweep",
                            "summary": "Says which open questions the week's reading answered.",
                            "applicability": "Use at the start of a week. Not for one question you already have in mind.",
                            "intent": intent.model_dump(mode="json"),
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Here is the shape it would run. Read it and tell me what to change.")])

    return FunctionModel(function=answer)


def _body(response: JSONResponse) -> dict[str, object]:
    """One route answer, decoded back to the JSON it went out as."""
    decoded = json.loads(bytes(response.body))
    assert isinstance(decoded, dict)
    return decoded


async def _offered(session: Runtime, process: WorkflowProcess) -> frozenset[str]:
    """Every tool name this turn's own surface would put in front of the model."""
    ctx: RunContext[Runtime] = RunContext(deps=session, model=TestModel(), usage=RunUsage())
    return frozenset(await process.get_toolset().get_tools(ctx))


async def test_a_design_turn_is_offered_no_verb_that_starts_work(tmp_path: Path, registry: StepRegistry) -> None:
    """Withheld, not discouraged: the tools that start a run or change a schedule are not on the surface at all."""
    session = _session(tmp_path, registry)
    session.design_only = True
    process = _process(tmp_path, session, FakeSchedules())

    offered = await _offered(session, process)

    assert CHECK_TOOL in offered, "a design turn still drafts"
    assert RUN_TOOL not in offered
    assert "schedule_workflow" not in offered
    assert offered.isdisjoint(STARTS_WORK), f"still offered {sorted(offered & STARTS_WORK)}"
    assert {"check_runs", "recent_runs", "schedules", "run_values", "stop_run"} <= offered, "the reads and the stop stay"


async def test_an_ordinary_turn_keeps_every_verb(tmp_path: Path, registry: StepRegistry) -> None:
    """The narrowing belongs to the turn that asked for it, and to no other."""
    session = _session(tmp_path, registry)
    process = _process(tmp_path, session, FakeSchedules())

    offered = await _offered(session, process)

    assert offered >= STARTS_WORK, f"missing {sorted(STARTS_WORK - offered)}"


async def test_the_narrowing_lasts_one_turn(tmp_path: Path, registry: StepRegistry) -> None:
    """One session designs on one turn and starts a run on the next, so the flag is written per turn."""
    session = _session(tmp_path, registry)
    process = _process(tmp_path, session, FakeSchedules())

    session.design_only = True
    assert RUN_TOOL not in await _offered(session, process)
    session.design_only = False
    assert RUN_TOOL in await _offered(session, process)


async def test_a_turn_that_says_it_only_designs_reaches_the_model_without_those_verbs(tmp_path: Path, registry: StepRegistry) -> None:
    """The whole path, from the body a browser posts to the tool list the model is actually handed."""
    offered: list[frozenset[str]] = []

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        offered.append(frozenset(tool.name for tool in info.function_tools))
        return ModelResponse(parts=[TextPart("Here is the shape it would run.")])

    session = _session(tmp_path, registry)
    process = _process(tmp_path, session, FakeSchedules())
    session.agent = Agent(streaming_function_model(answer), deps_type=Runtime, capabilities=[process])
    pool = SessionPool(session)
    chat = a_chat(pool)
    session_id = one_session(pool)

    await a_turn(chat, session_id, submit("Design me a Monday sweep", design_only=True))
    await a_turn(chat, session_id, submit("Now start it"))

    assert len(offered) == 2
    designing, ordinary = offered
    assert CHECK_TOOL in designing
    assert designing.isdisjoint(STARTS_WORK), f"a design turn was handed {sorted(designing & STARTS_WORK)}"
    assert ordinary >= STARTS_WORK, "the next turn in the same session keeps every verb"


async def test_a_design_turn_settles_a_draft_and_starts_nothing(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The negative half is the point: asking for a design reaches the checker and never the runner."""
    session = _session(tmp_path, registry)
    session.design_only = True
    store = FakeSchedules()
    process = _process(tmp_path, session, store)
    records = records_under(tmp_path)
    agent = Agent(_checks_the_draft(quarterly_report, ASKED_FOR), deps_type=Runtime, capabilities=[process])

    result = await agent.run("Design me a Monday sweep over my open questions", deps=session, usage_limits=UsageLimits(request_limit=4))

    assert "Read it" in result.output
    assert session.conversation.draft.intent == ASKED_FOR, "the request rides the settle"
    assert yaml.safe_load(session.conversation.draft.spec_yaml)["name"] == quarterly_report.name
    assert session.active.task is None, "a design turn leaves no run in flight"
    assert records.archive.recent(limit=10) == [], "nothing was ever run"
    assert store.rows == {}, "nothing was ever scheduled"


async def test_a_design_turn_tells_the_agent_which_answers_are_still_open(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The checker asks for the blanks rather than filling them, so the next turn can ask the person."""
    session = _session(tmp_path, registry)
    process = _process(tmp_path, session, FakeSchedules())
    agent = Agent(_checks_the_draft(quarterly_report, ASKED_FOR), deps_type=Runtime, capabilities=[process])

    result = await agent.run("design it", deps=session, usage_limits=UsageLimits(request_limit=4))

    checked = [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "check_draft" and isinstance(part.content, str)
    ]
    assert len(checked) == 1
    assert f"Still unanswered about what they asked for: {', '.join(STILL_OPEN)}" in checked[0]


def test_the_three_readings_of_one_draft_stay_apart(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Unanswered intent, undescribed card and an unbuildable graph are three answers, not one list."""
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)
    session = _session(tmp_path, registry, draft=Draft(spec_yaml=spec_yaml, title="Monday question sweep", intent=ASKED_FOR))

    payload = draft_payload(session)

    assert payload["intent_issues"] == STILL_OPEN
    readiness = payload["readiness"]
    assert isinstance(readiness, dict)
    findings = readiness["findings"]
    assert isinstance(findings, list)
    fields = {str(finding["field"]) for finding in findings if isinstance(finding, dict)}
    assert "summary" in fields, "the card reading is its own, and says the summary is missing"
    assert fields.isdisjoint(set(STILL_OPEN)), "no intent field leaks into the card reading"
    assert payload["issues"] == [], "this run can build every step, which is the third reading"


def test_a_draft_nobody_stated_an_intent_for_owes_nothing(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """No intent is not an incomplete intent: nothing is owed until somebody starts stating one."""
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)
    session = _session(tmp_path, registry, draft=Draft(spec_yaml=spec_yaml, title="Monday question sweep"))

    assert draft_payload(session)["intent_issues"] == []


def test_recording_an_intent_replaces_it_whole_and_moves_nothing_else(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The person's own edit: one statement in, the same proposal out."""
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)
    session = _session(tmp_path, registry, draft=Draft(spec_yaml=spec_yaml, title="Monday question sweep", intent=ASKED_FOR))

    draft = record_intent(session, EDITED)

    assert draft.intent == EDITED
    assert draft.spec_yaml == spec_yaml
    assert draft.title == "Monday question sweep"
    assert session.conversation.draft is draft


async def test_the_intent_route_keeps_the_edit_and_a_reload_reads_it_back(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """A reload is the whole point: an edited assumption survives one, off the session's own file."""
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)
    session = _session(tmp_path, registry, draft=Draft(spec_yaml=spec_yaml, title="Monday question sweep", intent=ASKED_FOR))
    session.session_id = "s1"
    controller = a_controller(SessionPool(session), state=tmp_path)

    answer = await controller.record_session_intent("s1", EDITED)

    assert answer.status_code == 200
    assert _body(answer)["intent_issues"] == STILL_OPEN
    reloaded = _body(await controller.session_conversation("s1"))
    draft = reloaded["draft"]
    assert isinstance(draft, dict)
    intent = draft["intent"]
    assert isinstance(intent, dict)
    assert intent["assumptions"] == ["Read only the knowledge base I pick at the start of the run"]
    kept = remembered_draft(path=tmp_path / "draft.json")
    assert kept is not None
    assert kept.intent == EDITED, "a restart reads back the person's own words"


async def test_the_intent_route_refuses_a_session_that_is_not_here(tmp_path: Path, registry: StepRegistry) -> None:
    session = _session(tmp_path, registry)
    session.session_id = "s1"
    controller = a_controller(SessionPool(session), state=tmp_path)

    answer = await controller.record_session_intent("nobody", ASKED_FOR)

    assert answer.status_code == 404
