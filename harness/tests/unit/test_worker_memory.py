"""Worker memory: a worker step keeps one conversation per run, and a rejection reaches it once.

Exercises the built steps directly (`prepare(...).registry.get(name).function`) -- the same seam
`test_worker_runs.py` and `tests/support/environment.py` already use, so a worker or a briefing is run
exactly as the indexer would run it, with `composition.build_environment`'s own `active` in hand to
inspect and mutate between calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    GateDecision,
    MapNode,
    ParameterSpec,
    PersonGate,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowArguments,
    WorkflowState,
    build_graph,
    prepare,
    run_workflow,
)
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.service import archive_run
from harness.session import ActiveRun, NoSessionLog, Runtime, SessionPool, bind_active
from tests.conftest import ApproveAll
from tests.support.environment import world_over
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.registry import StepImplementation
    from pydantic_ai.models.function import AgentInfo, FunctionModel

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}


def _worker_workflow(name: str = "weigh") -> Workflow:
    """One worker step, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="One worker step, run directly against a run-scoped `active`.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            AgentStep(
                name=name,
                agent={"instructions": "Read the ledger and say whether the quarter looks healthy."},
                inputs={"ledger": Shape(json_schema=LEDGER)},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(RunNode(id=name, work=name, inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node=name),
    )


def _task_workflow(name: str = "draft_task") -> Workflow:
    """One task briefing, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="draft-once",
        version=1,
        description="One task briefing, run directly against a run-scoped `active`.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            Briefing(
                name=name,
                instructions="Draft a short summary of the reconciled ledger, ready for a person to approve or send back.",
                inputs={"ledger": Shape(json_schema=LEDGER)},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(RunNode(id=name, work=name, inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node=name),
    )


async def _worker_step(model: FunctionModel, active: ActiveRun, tmp_path: Path, *, name: str = "weigh") -> StepImplementation:
    environment = await world_over(model, state=tmp_path, active=active)
    ready = prepare(_worker_workflow(name), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get(name)
    assert implementation is not None
    return implementation


async def _task_step(model: FunctionModel, active: ActiveRun, tmp_path: Path, *, name: str = "draft_task") -> StepImplementation:
    environment = await world_over(model, state=tmp_path, active=active)
    ready = prepare(_task_workflow(name), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get(name)
    assert implementation is not None
    return implementation


def _counting_model() -> tuple[FunctionModel, list[int]]:
    """A model that answers immediately and records how many messages it was handed each turn."""
    seen: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart(content=f"answer {len(messages)}")])

    return streaming_function_model(respond), seen


def _echoing_model() -> tuple[FunctionModel, list[str]]:
    """A model that records the user prompt it was asked, verbatim, and answers something fixed."""
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        part = messages[-1].parts[-1]
        prompts.append(str(part.content) if isinstance(part, UserPromptPart) else "")
        return ModelResponse(parts=[TextPart(content="ok")])

    return streaming_function_model(respond), prompts


# --- a worker keeps its own conversation across the run ---------------------------------------


async def test_a_second_invocation_of_the_same_worker_carries_the_first_ones_messages(tmp_path: Path) -> None:
    model, seen = _counting_model()
    active = ActiveRun()
    step = await _worker_step(model, active, tmp_path)

    await step.function(ledger={"net": 400})
    await step.function(ledger={"net": 400})

    assert seen[1] > seen[0], "the second call carried the first call's history forward"
    assert ("weigh", "") in active.conversations.conversations
    assert active.conversations.conversations[("weigh", "")].messages, "the run's own conversation is not empty"


async def test_resetting_conversations_between_runs_isolates_them(tmp_path: Path) -> None:
    model, seen = _counting_model()
    active = ActiveRun()
    step = await _worker_step(model, active, tmp_path)

    await step.function(ledger={"net": 400})
    await step.function(ledger={"net": 400})
    active.conversations.reset()
    await step.function(ledger={"net": 400})

    assert seen[2] == seen[0], "a reset run starts exactly as fresh as the very first one"
    assert active.conversations.conversations[("weigh", "")].messages, "the post-reset run still keeps its own new history"


# --- a worker step's line is executing while it runs, cleared even on a raise ------------------


async def test_a_worker_steps_line_is_executing_at_the_moment_the_model_is_asked(tmp_path: Path) -> None:
    active = ActiveRun()
    seen: list[bool] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        line = active.conversations.conversations.get(("weigh", ""))
        seen.append(line is not None and line.executing)
        return ModelResponse(parts=[TextPart(content=f"answer {len(messages)}")])

    step = await _worker_step(streaming_function_model(respond), active, tmp_path)

    await step.function(ledger={"net": 400})

    assert seen == [True], "the worker's line was open and executing at the moment the model was asked"
    assert active.conversations.conversations[("weigh", "")].executing is False, "settled once the run ends"


async def test_a_raising_worker_step_still_clears_executing(tmp_path: Path) -> None:
    active = ActiveRun()

    def always_raises(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        msg = "boom"
        raise RuntimeError(msg)

    step = await _worker_step(streaming_function_model(always_raises), active, tmp_path)

    with pytest.raises(RuntimeError):
        await step.function(ledger={"net": 400})

    line = active.conversations.conversations[("weigh", "")]
    assert line.executing is False, "the finally clears it even though the run raised"


# --- a line is keyed by node and item, not step name alone -------------------------------------


def _mapped_ledgers_workflow() -> Workflow:
    """A map fans one worker step out over a collection, each item its own turn."""
    return Workflow(
        name="weigh-every-ledger",
        version=1,
        description="A map runs one worker step once per ledger.",
        parameters={"ledgers": ParameterSpec(description="the ledgers", json_schema={"type": "array", "items": LEDGER})},
        inline=(
            AgentStep(
                name="weigh",
                agent={"instructions": "Read the ledger and say whether the quarter looks healthy."},
                inputs={"ledger": Shape(json_schema=LEDGER)},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(MapNode(id="weigh", turns=("weigh",), over=FromParameter(parameter="ledgers"), each="ledger"),),
        output=FromNode(node="weigh"),
    )


async def test_two_map_items_invoking_one_worker_step_keep_two_lines(tmp_path: Path) -> None:
    model, _seen = _counting_model()
    active = ActiveRun()
    environment = await world_over(model, state=tmp_path, active=active)

    await run_workflow(_mapped_ledgers_workflow(), environment, params={"ledgers": [{"net": 400}, {"net": 500}]}, approval=ApproveAll())

    weighing = [key for key in active.conversations.conversations if key[0] == "weigh"]
    assert len(weighing) == 2, "each item kept its own line"
    assert len({key[1] for key in weighing}) == 2, "under two distinct item keys"


async def test_a_plain_run_nodes_rerun_still_resumes_its_line(tmp_path: Path) -> None:
    model, seen = _counting_model()
    active = ActiveRun()
    environment = await world_over(model, state=tmp_path, active=active)
    spec = _worker_workflow()

    await run_workflow(spec, environment, params={"ledger": {"net": 400}}, approval=ApproveAll())
    await run_workflow(spec, environment, params={"ledger": {"net": 400}}, approval=ApproveAll())

    assert seen[1] > seen[0], "the second run carried the first run's history forward"
    assert set(active.conversations.conversations) == {("weigh", "")}, "a rerun resumes the one line, keyed by node with no item"


# --- a pooled session's own `active` is what a bound run reads and writes, not the default's ---


async def _shared_registry_and_default(model: FunctionModel, tmp_path: Path) -> tuple[StepRegistry, ActiveRun]:
    """One registry, closed over one default `active` -- exactly what every pooled session shares.

    `SessionPool._runtime_for` gives every pooled runtime the process's own registry, built once
    against the default session's `active`, and a fresh `ActiveRun` of its own. Building it this
    way, rather than through `Runtime`, is what lets a test bind a pooled session's `active` around
    the identical closures the default session's own calls fall back to.
    """
    active = ActiveRun()
    environment = await world_over(model, state=tmp_path, active=active)
    return environment, active


async def test_a_pooled_sessions_worker_conversation_lands_on_its_own_active_not_the_defaults(tmp_path: Path) -> None:
    model, seen = _counting_model()
    environment, default_active = await _shared_registry_and_default(model, tmp_path)
    ready = prepare(_worker_workflow(), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("weigh")
    assert implementation is not None
    pool = SessionPool(Runtime(registry=environment, library=cast("Any", None), agent=cast("Any", object()), active=default_active, session_log=NoSessionLog(), state=a_state(tmp_path)))
    pooled = await pool.create(None, session_kind="runner")

    with bind_active(pooled.active):
        await implementation.function(ledger={"net": 400})
        await implementation.function(ledger={"net": 400})

    assert seen[1] > seen[0], "the pooled run's second call still carried its own first call's history"
    assert pooled.active.conversations.conversations[("weigh", "")].messages, "the pooled session's own line holds the exchange"
    assert default_active.conversations.conversations == {}, "none of a pooled run's worker memory reaches the default session"


async def test_a_default_sessions_worker_conversation_never_reaches_a_pooled_sessions_active(tmp_path: Path) -> None:
    model, _seen = _counting_model()
    environment, default_active = await _shared_registry_and_default(model, tmp_path)
    ready = prepare(_worker_workflow(), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("weigh")
    assert implementation is not None
    pool = SessionPool(Runtime(registry=environment, library=cast("Any", None), agent=cast("Any", object()), active=default_active, session_log=NoSessionLog(), state=a_state(tmp_path)))
    pooled = await pool.create(None, session_kind="runner")

    await implementation.function(ledger={"net": 400})  # unbound: exactly a direct default-session call

    assert default_active.conversations.conversations[("weigh", "")].messages
    assert pooled.active.conversations.conversations == {}, "the default session's own run never reaches a pooled session's active"


async def test_a_briefing_carries_no_history_across_invocations(tmp_path: Path) -> None:
    """The brief's own ruling: task briefings stay one-shot, unlike a worker."""
    model, seen = _counting_model()
    active = ActiveRun()
    step = await _task_step(model, active, tmp_path)

    await step.function(ledger={"net": 400})
    await step.function(ledger={"net": 400})

    assert seen[1] == seen[0], "a second briefing call sees exactly as much history as the first: none"
    line = active.conversations.conversations[("draft_task", "")]
    assert line.messages, "a briefing's line now holds its latest run, even though it stays one-shot"
    assert line.executing is False


# --- the archive record carries the run's conversations ---------------------------------------


def _archive_session(tmp_path: Path) -> Runtime:
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


def _minimal_spec() -> Workflow:
    return Workflow(
        name="quarterly-report",
        version=1,
        description="A minimal spec for the archive test.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="reconcile", work="reconcile", inputs={}),),
        output=FromNode(node="reconcile"),
    )


def test_the_archive_record_round_trips_a_runs_conversations(tmp_path: Path) -> None:
    records = records_under(tmp_path)
    session = _archive_session(tmp_path)
    session.session_id = "sess-1"
    session.active.workflow_id = "wf.weigh"
    session.active.started_at = "2026-01-01T00:00:00+00:00"
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="net: 400")]), ModelResponse(parts=[TextPart(content="healthy")])]

    archive_run(
        session,
        archive=records.archive,
        outcome="completed",
        failure="",
        runner="app:harness",
        spec=_minimal_spec(),
        params={"quarter": "Q2"},
        values={},
        conversations={("weigh", ""): messages},
        gates_approved=(),
        gates_rejected={},
        gates_auto=(),
    )

    archived = records.archive.recent(10)
    record = records.archive.get(archived[0].run_id)
    assert record is not None
    assert set(record.conversations) == {"weigh[]"}
    assert [entry["kind"] for entry in record.conversations["weigh[]"]] == ["request", "response"]


def test_a_legacy_record_with_no_conversations_field_still_parses() -> None:
    """A record archived before workers kept memory carries none -- it must still parse."""
    legacy = {
        "run_id": "run-legacy",
        "workflow_id": "wf.report",
        "outcome": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:01:00+00:00",
        "runner": "app:harness",
        "session_id": "sess-1",
        "params": {"quarter": "Q2"},
        "spec": {"name": "quarterly-report"},
        "values": {"reconcile": {"net": 400}},
        "events": [],
    }

    record = RunArchiveRecord.model_validate(legacy)

    assert record.conversations == {}


# --- a rejection's own evidence reaches the rerun that owes the repair --------------------------


def _repairable(original: Workflow) -> Workflow:
    """`original` with a gate over its first node, rejecting back into it."""
    source = original.nodes[0]
    gate = PersonGate(id="review", reviews=FromNode(node=source.id), rejected_to=source.id)
    return original.model_copy(update={"nodes": (*original.nodes, gate), "output": FromNode(node="review")})


def _two_nodes_one_step() -> Workflow:
    """Two nodes running the same step, the second reading what the gate over the first released.

    Strictly ordered: the second node runs only after the first has been repaired and approved,
    which is what makes "it heard nothing" mean something.
    """
    text = Shape(json_schema={"type": "string"})
    return Workflow(
        name="one-step-twice",
        version=1,
        description="One worker step run by two nodes, a gate between them.",
        parameters={"text": ParameterSpec(description="what to weigh", json_schema={"type": "string"})},
        inline=(
            AgentStep(
                name="weigh",
                agent={"instructions": "Read what you are given and say whether it holds up."},
                inputs={"text": text},
                output=text,
            ),
        ),
        nodes=(
            RunNode(id="first", work="weigh", inputs={"text": FromParameter(parameter="text")}),
            PersonGate(id="review", reviews=FromNode(node="first"), rejected_to="first"),
            RunNode(id="second", work="weigh", inputs={"text": FromNode(node="review")}),
        ),
        output=FromNode(node="second"),
    )


class _RefuseOnce:
    """Refuses the first value it is shown and releases the next."""

    reached = 0

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        del gate_id, value, note
        self.reached += 1
        return GateDecision(approve=self.reached > 1, feedback="cite the constraint")


@pytest.mark.parametrize("mapped", [False, True])
async def test_a_rerun_is_asked_over_the_value_it_was_refused_and_the_request_it_started_from(tmp_path: Path, mapped: bool) -> None:
    """One message, carrying the original request, the refused value, and what to change.

    Every item of a mapped rerun gets it, because every item is repairing the same rejection.
    """
    model, prompts = _echoing_model()
    environment = await world_over(model, state=tmp_path, active=ActiveRun())
    spec = _repairable(_mapped_ledgers_workflow() if mapped else _worker_workflow())
    params: WorkflowArguments = {"ledgers": [{"net": 400}, {"net": 500}]} if mapped else {"ledger": {"net": 400}}

    await build_graph(spec, environment, approval=_RefuseOnce()).run(state=WorkflowState(params=params))

    width = 2 if mapped else 1
    assert len(prompts) == width * 2, "every item ran once, then once more to repair"
    assert all("cite the constraint" not in prompt for prompt in prompts[:width]), "nothing was refused yet"
    for prompt in prompts[width:]:
        assert "Sent back at gate review (attempt 1, decided by person)" in prompt
        assert "What was reviewed:" in prompt
        assert "ok" in prompt, "the answer the gate actually refused"
        assert "The request this run was given" in prompt
        assert '"net":400' in prompt.replace(" ", ""), "the parameters the run started from"
        assert "What to change: cite the constraint" in prompt


async def test_a_second_node_running_the_same_step_is_asked_to_repair_nothing(tmp_path: Path) -> None:
    """The repair belongs to the node the gate reruns, never to the step that node happens to run."""
    model, prompts = _echoing_model()
    environment = await world_over(model, state=tmp_path, active=ActiveRun())

    await build_graph(_two_nodes_one_step(), environment, approval=_RefuseOnce()).run(state=WorkflowState(params={"text": "a claim"}))

    assert len(prompts) == 3, "the first node, its repair, then the second node once"
    assert [("cite the constraint" in prompt) for prompt in prompts] == [False, True, False]


async def test_a_turn_that_stopped_leaves_its_own_history_for_the_next_call(tmp_path: Path) -> None:
    """A failed turn never settles its line, so without keeping it the next call would repeat it blind."""
    received: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        received.append(list(messages))
        if len(received) == 1:
            msg = "The provider stopped this turn."
            raise RuntimeError(msg)
        return ModelResponse(parts=[TextPart(content="recovered")])

    step = await _worker_step(streaming_function_model(respond), ActiveRun(), tmp_path)

    with pytest.raises(RuntimeError, match="provider stopped"):
        await step.function(ledger={"net": 400})
    await step.function(ledger={"net": 500})

    assert len(received[1]) > len(received[0]), "the second call carried the stopped turn's own exchange forward"
    assert "400" in str(received[1]), "including what the stopped turn was asked"
    assert "500" in str(received[1])
