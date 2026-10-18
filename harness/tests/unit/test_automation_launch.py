"""The whole launch path a person walks: preview, one deliberate start, reuse, and the reads that settle doubt.

Seven cases, one per row of the launch contract. Values are admitted or refused before anything
runs, a second start in a session already starting one is refused rather than duplicated, a start
whose answer never reached the browser is settled by reading the session's own runs and the recent
history, a rerun is another deliberate start that leaves the first record alone, an adaptation is a
child draft that never touches its parent or the run it cites, and a turn that only designs starts
nothing at all.

Everything here runs over the real controllers with fakes behind them, so what a browser posts,
what a step is handed, and what the archive keeps stay one round trip rather than three assertions
about three layers.
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    InputPresentation,
    Outcome,
    ParameterSpec,
    RunNode,
    SavedWorkflow,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowPresentation,
    run_workflow,
)
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from httpx import ASGITransport
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from harness.adapters.knowledge_base import knowledge_base_names
from harness.features.authoring.domain import SourceRun
from harness.features.authoring.process import STARTS_WORK, WorkflowProcess
from harness.features.running.contracts import SpendLimit
from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime, SessionPool
from harness.settings import RunSettings
from tests.conftest import ApproveAll
from tests.support.authoring import a_controller
from tests.support.controllers import a_client_of, mounted
from tests.support.environment import world_over
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.studio_registry import FakeStudioApi, registry_row
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.running import a_running_controller
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model
from tests.unit.test_schedule_ticker import FakeWorkflowStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from core.ai.workflows.contracts import Tier
    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo

    from harness.features.running.controller import RunningController

WORKFLOW_ID = "wf.brief"
KNOWLEDGE_BASES = "knowledge_bases"
REGISTERED = (registry_row("research", None), registry_row("repo-docs", None))
"""The two knowledge bases this deployment answers the registry with."""


class Brief(BaseModel):
    """What the one step answers with, so a run has something to collect."""

    question: str


class Handed(BaseModel):
    """Exactly what the step was handed, kept for a case to read back."""

    question: str
    knowledge_bases: list[str]


def _spec() -> Workflow:
    """One question, one list of knowledge bases, one step. A shape, carrying nobody's vocabulary."""
    return Workflow(
        name="brief",
        version=1,
        description="Read the named knowledge bases and brief the person on one decision.",
        parameters={
            "question": ParameterSpec(description="the decision to take", json_schema={"type": "string", "minLength": 1}),
            KNOWLEDGE_BASES: ParameterSpec(
                description="which knowledge bases to read",
                json_schema={"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 3},
            ),
        },
        nodes=(
            RunNode(
                id="brief",
                work="write_brief",
                inputs={"question": FromParameter(parameter="question"), KNOWLEDGE_BASES: FromParameter(parameter=KNOWLEDGE_BASES)},
            ),
        ),
        output=FromNode(node="brief"),
    )


CARD = SavedWorkflow(
    id=WORKFLOW_ID,
    title="Decision brief",
    summary="Read the named knowledge bases and brief the person on one decision.",
    presentation=WorkflowPresentation(
        inputs=(
            InputPresentation(key="question", label="The decision", control="textarea"),
            InputPresentation(key=KNOWLEDGE_BASES, label="Knowledge bases", control="knowledge-base"),
        )
    ),
)
"""The saved card, where the first-party selector is declared."""

GOOD: dict[str, JsonValue] = {"question": "Which cache?", KNOWLEDGE_BASES: ["research", "repo-docs"]}
"""Every declared parameter, each as the JSON value a person actually chose."""


def _registry(seen: list[Handed]) -> StepRegistry:
    """A registry whose one step records exactly what the run handed it."""
    registry = StepRegistry()

    async def write_brief(question: str, knowledge_bases: list[str]) -> Brief:
        seen.append(Handed(question=question, knowledge_bases=knowledge_bases))
        return Brief(question=question)

    registry.register(write_brief)
    return registry


def _library() -> FakeWorkflowStore:
    """A library holding the brief and its card."""
    return FakeWorkflowStore(specs={WORKFLOW_ID: _spec()}, cards={WORKFLOW_ID: CARD})


class SuspendingLibrary(FakeWorkflowStore):
    """The same library, whose reads actually hand the event loop back before answering.

    A fake whose coroutines never await runs to completion inside the caller's own step, so two
    requests arriving together would never interleave and a race between them could not show. This
    one suspends exactly where the real library does, which is what puts the second start inside
    the window the first has not left yet.
    """

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        await asyncio.sleep(0)
        return await super().fetch_workflow(workflow_id)

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        await asyncio.sleep(0)
        return await super().fetch_card(workflow_id)


def _controller(
    registry: StepRegistry,
    home: Path,
    *,
    library: FakeWorkflowStore | None = None,
    spend_limits: tuple[SpendLimit, ...] = (SpendLimit(window="run", usd=Decimal(2)), SpendLimit(window="day", usd=Decimal(25), warn_at=0.8)),
) -> tuple[RunningController, SessionPool]:
    """The running controller over a pool holding the brief, and that pool."""
    default = a_runtime(registry=registry, library=library if library is not None else _library(), state=a_state(home))
    pool = SessionPool(default)
    controller = a_running_controller(pool, home, knowledge_bases=knowledge_base_names(FakeStudioApi(rows=list(REGISTERED))), spend_limits=spend_limits)
    return controller, pool


async def _client(registry: StepRegistry, home: Path) -> AsyncIterator[tuple[httpx.AsyncClient, SessionPool]]:
    """A client over the running controller alone, and the pool its launches land in."""
    controller, pool = _controller(registry, home)
    async with a_client_of(controller) as client:
        yield client, pool


async def _finished(pool: SessionPool, session_id: str) -> str:
    """Let the run launched in `session_id` finish, and answer the id it was recorded under."""
    session = await pool.get(session_id)
    assert session.active.task is not None
    await session.active.task
    return session.active.run_id or ""


# -- the values a start admits ---------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "supplied", "admitted", "names"),
    [
        ("a knowledge base nothing here is registered under", {KNOWLEDGE_BASES: ["research", "nowhere"]}, False, "$[1]"),
        ("an array of two registered names", {KNOWLEDGE_BASES: ["research", "repo-docs"]}, True, ""),
    ],
)
async def test_what_a_start_admits_is_decided_before_anything_runs(
    case: str, supplied: dict[str, JsonValue], admitted: bool, names: str, tmp_path: Path
) -> None:
    """A refusal names the parameter and the path inside its value, and starts nothing at all."""
    del case
    seen: list[Handed] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        answer = await client.post("/api/run", json={"workflow_id": WORKFLOW_ID, "arguments": {**GOOD, **supplied}})

        if not admitted:
            assert answer.status_code == 400, answer.text
            assert KNOWLEDGE_BASES in answer.json()["message"]
            assert names in answer.json()["message"]
            assert seen == [], "nothing ran"
            assert pool.default.active.task is None, "no run was started in any session this test holds"
            return

        assert answer.status_code == 200, answer.text
        await _finished(pool, answer.json()["session_id"])
        assert seen[0].knowledge_bases == ["research", "repo-docs"], "the very array the person chose, never its text"


# -- one deliberate start --------------------------------------------------------------


async def test_a_second_start_while_one_is_pending_leaves_one_run(tmp_path: Path) -> None:
    """Two clicks on one session's Run: the session's own hold admits the first and refuses the second."""
    seen: list[Handed] = []
    controller, pool = _controller(
        _registry(seen), tmp_path, library=SuspendingLibrary(specs={WORKFLOW_ID: _spec()}, cards={WORKFLOW_ID: CARD})
    )
    async with a_client_of(controller) as client:
        created = await pool.create(None, session_kind="runner")
        path = f"/api/sessions/{created.session_id}/run"
        body = {"workflow_id": WORKFLOW_ID, "arguments": GOOD}

        both = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))

        assert sorted(answer.status_code for answer in both) == [200, 409], [answer.text for answer in both]
        refused = next(answer for answer in both if answer.status_code == 409).json()["message"]
        assert "busy" in refused, refused
        assert "nothing was started" in refused, refused
        assert "again" not in refused, "a session busy with a chat turn was never asked to start anything twice"
        await _finished(pool, created.session_id or "")
        assert len(seen) == 1, "one start request, one run"
        assert len(records_under(tmp_path).archive.list_for(WORKFLOW_ID, 10)) == 1


ANSWER_LOST = "the answer never arrived"
"""What a connection that dropped between the request and its answer reads as."""


class _LosesTheAnswer(httpx.AsyncBaseTransport):
    """A transport that delivers one path's request in full and then loses the answer coming back.

    The application handles the request to completion, so whatever that request started is started.
    What never arrives is the answer, which is exactly the state a browser is left in when the
    connection drops between the two.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, *, path: str) -> None:
        self._inner = inner
        self._path = path
        self.dropped = 0
        """How many answers were read whole and then thrown away."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Answer as the application does, unless this is the path whose answer is lost.

        Raises:
            httpx.ReadError: Always, for the one path under test, after the application answered.
        """
        answer = await self._inner.handle_async_request(request)
        if request.url.path != self._path:
            return answer
        await answer.aread()
        self.dropped += 1
        raise httpx.ReadError(ANSWER_LOST)


async def test_a_lost_start_response_is_settled_by_reading_the_runs_rather_than_starting_again(tmp_path: Path) -> None:
    """The start landed and its answer did not. Both reads name the run, and nothing started a second."""
    seen: list[Handed] = []
    controller, pool = _controller(_registry(seen), tmp_path)
    created = await pool.create(None, session_kind="runner")
    session_id = created.session_id or ""
    losing = _LosesTheAnswer(ASGITransport(app=mounted(controller)), path=f"/api/sessions/{session_id}/run")

    async with httpx.AsyncClient(transport=losing, base_url="http://test") as client:
        with pytest.raises(httpx.ReadError):
            await client.post(f"/api/sessions/{session_id}/run", json={"workflow_id": WORKFLOW_ID, "arguments": GOOD})
        assert losing.dropped == 1, "the start was answered, and the answer was the thing that went missing"

        live = await client.get("/api/runs")
        assert [view["session_id"] for view in live.json()["runs"]] == [session_id], "the session's own run, while it is in flight"

        await _finished(pool, session_id)
        recent = await client.get("/api/runs/recent")

        assert [record["session_id"] for record in recent.json()["runs"]] == [session_id], "and the same one run once it ended"
        assert len(seen) == 1, "reconciling reads, it never starts anything"


async def test_a_rerun_is_a_second_deliberate_start_and_leaves_the_first_record_alone(tmp_path: Path) -> None:
    """The same workflow, one input changed. Two records, each holding the values its own run was given."""
    seen: list[Handed] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        first = await client.post("/api/run", json={"workflow_id": WORKFLOW_ID, "arguments": GOOD})
        first_run = await _finished(pool, first.json()["session_id"])

        again = await client.post("/api/run", json={"workflow_id": WORKFLOW_ID, "arguments": {**GOOD, "question": "Which queue?"}})
        second_run = await _finished(pool, again.json()["session_id"])

        archive = records_under(tmp_path).archive
        kept = archive.get(first_run)
        assert kept is not None
        assert kept.params == GOOD, "the first run's own values, untouched by the rerun"
        newer = archive.get(second_run)
        assert newer is not None
        assert newer.params["question"] == "Which queue?"
        assert [handed.question for handed in seen] == ["Which cache?", "Which queue?"]


# -- what the preview says a run is held to --------------------------------------------


async def test_the_preview_reads_this_deployments_own_brakes_and_gate_policy(tmp_path: Path) -> None:
    controller, _ = _controller(_registry([]), tmp_path)
    async with a_client_of(controller) as client:
        limits = await client.get("/api/run/limits")

    assert limits.status_code == 200, limits.text
    body = limits.json()
    assert body["brakes"] == [
        {"window": "run", "usd": "2", "warn_at": None},
        {"window": "day", "usd": "25", "warn_at": 0.8},
    ]
    assert body["gate_modes"] == ["wait", "orchestrate", "auto"]
    assert body["default_gate_mode"] == "wait"


async def test_a_deployment_with_no_brake_configured_claims_no_ceiling(tmp_path: Path) -> None:
    """No brake is not a ceiling of zero. The reply says there is none, and invents no figure."""
    controller, _ = _controller(_registry([]), tmp_path, spend_limits=())
    async with a_client_of(controller) as client:
        limits = await client.get("/api/run/limits")

    assert limits.json()["brakes"] == []


# -- reuse -----------------------------------------------------------------------------


SOURCE_RUN = SourceRun(
    run_id="20260916T090000000000-abcd1234",
    workflow_id=WORKFLOW_ID,
    run_title="Decision brief, which cache",
    outcome=Outcome(status="partial", summary="One of the two sections came back empty.", artifacts=()),
)
"""The ended run an adaptation cites as the reason it exists."""


def _archived(record_id: str) -> RunArchiveRecord:
    """One ended run in the archive, as the adaptation's source."""
    return RunArchiveRecord(
        run_id=record_id,
        workflow_id=WORKFLOW_ID,
        run_title=SOURCE_RUN.run_title,
        outcome="completed",
        result=SOURCE_RUN.outcome,
        started_at="2026-09-16T09:00:00+00:00",
        ended_at="2026-09-16T09:06:00+00:00",
        runner="app:harness",
        session_id="s-1",
        params=GOOD,
        spec=_spec().model_dump(mode="json"),
        values={},
        events=[],
    )


async def test_an_adaptation_is_a_child_that_keeps_its_parent_and_cites_the_run_behind_it(tmp_path: Path) -> None:
    """The child carries the source run, what that run was observed to do, and the owner's own words."""
    library = _library()
    records = records_under(tmp_path)
    records.archive.put(_archived(SOURCE_RUN.run_id))
    default = a_runtime(registry=_registry([]), library=library, state=a_state(tmp_path))
    pool = SessionPool(default)
    created = await pool.create(None, session_kind="runner")
    controller = a_controller(pool, state=tmp_path, archive=records.archive)

    async with a_client_of(controller) as client:
        adapted = await client.post(
            f"/api/sessions/{created.session_id}/adapt",
            json={
                "workflow_id": WORKFLOW_ID,
                "ops": [{"op": "set_description", "to": "Brief the person, and say when the reading was too thin to answer."}],
                "source_run_id": SOURCE_RUN.run_id,
                "feedback": "The second section was empty and it did not say so.",
            },
        )

    assert adapted.status_code == 200, adapted.text
    lineage = adapted.json()["lineage"]
    assert lineage["parent_name"] == "brief"
    assert lineage["parent_version"] == 1
    assert SOURCE_RUN.run_id in lineage["evidence"], "the run behind the change is named"
    assert "partial" in lineage["evidence"], "and what that run was observed to do"
    assert "The second section was empty" in lineage["evidence"], "and the owner's own reason"

    child = yaml.safe_load(created.conversation.draft.spec_yaml)
    assert child["version"] == 2
    assert (await library.fetch_workflow(WORKFLOW_ID)).version == 1, "the parent is never written back"
    assert (await library.fetch_workflow(WORKFLOW_ID)).description == _spec().description
    kept = records.archive.get(SOURCE_RUN.run_id)
    assert kept is not None
    assert kept.result == SOURCE_RUN.outcome, "the run record the child cites is left exactly as it was"


async def test_an_adaptation_keeps_what_the_caller_said_beside_the_run_it_cites(tmp_path: Path) -> None:
    """Citing a run adds to the evidence the caller sent. It never quietly takes its place."""
    records = records_under(tmp_path)
    records.archive.put(_archived(SOURCE_RUN.run_id))
    pool = SessionPool(a_runtime(registry=_registry([]), library=_library(), state=a_state(tmp_path)))
    created = await pool.create(None, session_kind="runner")
    controller = a_controller(pool, state=tmp_path, archive=records.archive)

    async with a_client_of(controller) as client:
        adapted = await client.post(
            f"/api/sessions/{created.session_id}/adapt",
            json={
                "workflow_id": WORKFLOW_ID,
                "ops": [{"op": "set_description", "to": "Brief the person, and say when the reading was too thin."}],
                "source_run_id": SOURCE_RUN.run_id,
                "evidence": "Two readers asked for the empty sections to be named.",
            },
        )

    assert adapted.status_code == 200, adapted.text
    evidence = adapted.json()["lineage"]["evidence"]
    assert SOURCE_RUN.run_id in evidence, "the run behind the change is named"
    assert "Two readers asked for the empty sections to be named." in evidence, "and so is what the caller sent"


async def test_an_adaptation_naming_a_run_nothing_archived_is_refused(tmp_path: Path) -> None:
    """A citation of a run this deployment cannot read is not evidence, so the child is not made."""
    default = a_runtime(registry=_registry([]), library=_library(), state=a_state(tmp_path))
    pool = SessionPool(default)
    created = await pool.create(None, session_kind="runner")
    controller = a_controller(pool, state=tmp_path, archive=records_under(tmp_path).archive)

    async with a_client_of(controller) as client:
        refused = await client.post(
            f"/api/sessions/{created.session_id}/adapt",
            json={"workflow_id": WORKFLOW_ID, "ops": [{"op": "set_description", "to": "Say less."}], "source_run_id": "no-such-run"},
        )

    assert refused.status_code == 404, refused.text
    assert created.conversation.draft.spec_yaml == "", "nothing was proposed"


# -- a turn that only designs ----------------------------------------------------------


def _design_session(tmp_path: Path, registry: StepRegistry) -> Runtime:
    """One session asked for a design and nothing else."""
    session = a_runtime(registry=registry, state=a_state(tmp_path))
    session.design_only = True
    return session


async def test_a_design_turn_starts_no_run_and_creates_no_schedule(tmp_path: Path) -> None:
    """The verbs that start work are off the surface, and the stores behind them stay empty."""
    registry = _registry([])
    session = _design_session(tmp_path, registry)
    schedules = FakeSchedules()
    pool = SessionPool(session)
    process = WorkflowProcess(
        port=WorkflowContract[Runtime](
            source=folder_toolset(tmp_path / "library"), vocabulary=FOLDER, expose=(), registry=registry, saved_by="test"
        ),
        records=records_under(tmp_path),
        reporter=SpendReporter(FakeTelemetry()),
        scheduler=Scheduler(schedules, schedules, FakeBroadcaster()),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
        run=RunSettings(),
    )
    ctx: RunContext[Runtime] = RunContext(deps=session, model=TestModel(), usage=RunUsage())

    offered = frozenset(await process.get_toolset().get_tools(ctx))

    assert offered.isdisjoint(STARTS_WORK), f"still offered {sorted(offered & STARTS_WORK)}"
    assert session.active.task is None, "no run"
    assert schedules.rows == {}, "no schedule"


# -- the budget seam every generative model of a launch passes through -----------------


ROLES: tuple[Tier, ...] = ("worker", "orchestrator", "helper")
"""Every role a step agent can be built for, each answered by its own model."""


def _labelled(label: str) -> Model:
    """A model that answers every briefing with the label it was built under, so a reader can tell which answered."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": f"answered by {label}"})])

    return streaming_function_model(respond)


class _Wrapping:
    """A wrapper hook that records which role it was asked for and answers a model naming that role."""

    def __init__(self) -> None:
        self.roles: list[Tier] = []

    def __call__(self, model: Model, *, role: Tier) -> Model:
        del model
        self.roles.append(role)
        return _labelled(f"wrapped-{role}")


def _step_on(role: Tier) -> Workflow:
    """One invented step, run on the role its node names, so which model answered is the only question."""
    task = Briefing(
        name="dig",
        instructions="Answer the question the caller asked in one short note, and add nothing it did not ask for.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
    )
    return Workflow(
        name="dig-once",
        version=1,
        description="One invented step, so the model behind its role is the only thing under test.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(task,),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}, tier=role),),
        output=FromNode(node="dig"),
    )


async def _answered_by(environment: StepRegistry, role: Tier) -> str:
    """What the model a step agent of `role` was built over actually answered."""
    return str(await run_workflow(_step_on(role), environment, params={"question": "Which cache?"}, approval=ApproveAll()))


@pytest.mark.parametrize("role", ROLES)
async def test_every_role_a_step_agent_answers_on_passes_through_the_wrapper(role: Tier, tmp_path: Path) -> None:
    """The seam a measured pilot mounts on: the hook is asked for every role, and its answer is the model that runs."""
    wrapping = _Wrapping()

    environment = await world_over(_labelled("plain"), state=tmp_path, wrap_model=wrapping)

    assert sorted(wrapping.roles) == ["helper", "orchestrator", "worker"]
    assert f"answered by wrapped-{role}" in await _answered_by(environment, role)


@pytest.mark.parametrize("role", ROLES)
async def test_no_wrapper_leaves_every_role_on_the_model_it_was_built_with(role: Tier, tmp_path: Path) -> None:
    """`None` is the production case: every role answers on the model the environment was built with."""
    environment = await world_over(_labelled("plain"), state=tmp_path)

    assert "answered by plain" in await _answered_by(environment, role)
