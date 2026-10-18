"""A multi-turn map whose line lives in the record: a real server, a real Postgres, two separate workers.

The claim this module exists for is the one the in-process engine cannot make. In this process a
later turn resumes its item out of `StepConversations`, which dies with the process. Here turn 1
is performed by one environment, that environment is dropped, and turn 2 is performed by a second
one built from scratch -- which resumes turn 1's exact messages because the record holds them and
the ledger says where.

Nothing is mocked. The first test runs a whole workflow on a Temporal dev server, so the payload
converter, the workflow sandbox and the activity boundary are all real. The rest drive the very
activity that server calls, with real `ActivityInfo` and a real record behind it, because what they
assert -- a second worker, a crash after commit, a refusal -- is about which delivery sees what,
and the test has to say which delivery is which.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from core.ai.history.contracts import (
    ContinuationAttempt,
    ContinuationInFlightError,
    ContinuationRef,
    PoolDatabase,
    PostgresContinuationStore,
    PostgresStepStore,
    create_record_schema,
)
from core.ai.workflows.contracts import (
    Briefing,
    DurableWorkflowRun,
    FromNode,
    FromParameter,
    GateVerdict,
    MapNode,
    ParameterSpec,
    PersonGate,
    RunNode,
    RunRequest,
    Shape,
    StepRegistry,
    Workflow,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.temporal import (
    PERFORM_INLINE_STEP,
    REFUSED_CONTINUATION,
    IdentityOnTheWire,
    InlineStepCall,
    InlineStepOutcome,
    TurnOnTheWire,
    continuation_attempt_id,
    continuation_conversation,
    sandbox_runner,
    step_activities,
)
from core.ai.workflows.infrastructure.temporal import bind as bind_registry
from engram.testing.infrastructure.knowledge_base_db import (
    pg,  # noqa: F401 -- imported to be used as a pytest fixture, never called directly
)
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ToolEffectRecord
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

from harness.adapters.store import RecordDatabase
from harness.adapters.wiring import open_record_pool
from harness.composition import build_environment
from harness.features.running.api_models import Decision
from harness.features.running.controller import run_decide
from harness.features.running.domain.progress import PendingReview
from harness.features.running.infrastructure.durable import DurableTurns
from harness.session import ActiveRun, NoSessionLog
from harness.settings import RunSettings, WebSearchSettings
from tests.integration.conftest import as_asyncpg_dsn
from tests.support.environment import fake_engram
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path
    from uuid import UUID

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from temporalio.client import Client, WorkflowHandle
    from temporalio.testing import WorkflowEnvironment


BOUNDED = ActivityConfig(start_to_close_timeout=timedelta(seconds=30))
"""How a server-backed test here schedules a step: a ceiling and nothing else.

Deliberately no retry policy, so Temporal's own unbounded default applies. A refusal that failed
retryably would spin under this config rather than end the run, which is exactly what the refusal
test would then hang on."""

@pytest.fixture
def execution() -> str:
    """One workflow execution of this test's own, so its lines cannot be another test's.

    The record is shared across the tier, and a line is derived from the execution it belongs to,
    so a fixed id here would let one test settle a conversation the next one expects untouched.
    """
    return f"execution-{uuid4().hex}"

ITEM_SCHEMA = {"type": "object", "title": "Item", "properties": {"title": {"type": "string"}}, "required": ["title"]}
TRIAGE_INSTRUCTIONS = "Read the item and write one short triage note about it."
DRAFT_INSTRUCTIONS = "Using your triage note, write a short draft about the item."

TRIAGE = Briefing(
    name="triage",
    instructions=TRIAGE_INSTRUCTIONS,
    inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
    output=Shape(json_schema={"type": "string", "title": "TriageNote"}),
)
DRAFT = Briefing(
    name="draft",
    instructions=DRAFT_INSTRUCTIONS,
    inputs={"triage": SameAs(like="triage")},
    output=Shape(json_schema={"type": "string", "title": "DraftText"}),
)

DIGEST = Workflow(
    name="digest-each-item",
    version=1,
    description="Take every item through triage and then a draft, as one conversation per item.",
    parameters={"items": ParameterSpec(description="items to digest", json_schema={"type": "array", "items": ITEM_SCHEMA})},
    inline=(TRIAGE, DRAFT),
    nodes=(MapNode(id="digest_each", turns=("triage", "draft"), over=FromParameter(parameter="items"), each="item"),),
    output=FromNode(node="digest_each"),
)


@pytest_asyncio.fixture(loop_scope="module")
async def module_record(pg: str) -> AsyncIterator[RecordDatabase]:  # noqa: F811 -- pytest resolves the imported `pg` fixture by its registered name, so the parameter must keep it
    """The record's own pool, opened on the loop the worker and its activities run on.

    The tier's shared `record_database` is born on a per-test loop, and asyncpg binds a pool to the
    loop that created it. A worker serving a module-scoped loop would then reach a pool belonging to
    another one, so the server-backed test gets its own.
    """
    dsn = as_asyncpg_dsn(pg)
    sessions = await asyncpg.create_pool(dsn)
    if sessions is None:
        pytest.skip(f"no Postgres at {dsn}")
    try:
        pool = await open_record_pool(sessions, dsn=dsn)
        await create_record_schema(PoolDatabase(pool))
        try:
            yield RecordDatabase(pool)
        finally:
            await pool.close()
    finally:
        await sessions.close()


class Asked:
    """Every model request this environment answered, and what it was asked with."""

    def __init__(self) -> None:
        self.calls: list[list[ModelMessage]] = []
        self.instructions: list[str | None] = []

    def answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """Answer with this turn's own text, recording the history and system prompt it arrived under."""
        self.calls.append(list(messages))
        self.instructions.append(info.instructions)
        turn = sum(1 for message in messages for part in message.parts if isinstance(part, UserPromptPart)) - 1
        return ModelResponse(parts=[TextPart(f"answer for turn {turn}")])

    def prompts_of(self, call: int) -> list[str]:
        """Every user prompt the request at `call` carried, oldest first."""
        return [
            part.content
            for message in self.calls[call]
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]


async def _an_environment(record: RecordDatabase, home: Path, asked: Asked, active: ActiveRun) -> StepRegistry:
    """One worker's whole environment: its own run state, its own model, and the shared record behind both."""
    database = PoolDatabase(record.pool)
    return await build_environment(
        streaming_function_model(asked.answer),
        engram=fake_engram(),
        active=active,
        external_agents=dict,
        session_log=NoSessionLog(),
        custom=home / "custom",
        records=records_under(home),
        web_search=WebSearchSettings(),
        tiers=tiers_under(home),
        turns=DurableTurns(checkpoints=PostgresContinuationStore(database), record=PostgresStepStore(database)),
    )


def _brought_activity(registry: StepRegistry) -> Callable[[InlineStepCall], Awaitable[object]]:
    """The one activity a brought step reaches, taken off the very table a worker would publish."""
    # The name is read off the dunder Temporal stamps on the callable it returns, which no type declares.
    return next(
        published
        for published in step_activities(registry)
        if getattr(published, "__temporal_activity_definition").name == PERFORM_INLINE_STEP
    )


def _a_delivery(execution: str, *, activity_id: str) -> ActivityEnvironment:
    """One delivery of one scheduled activity, as Temporal would describe it to the activity."""
    environment = ActivityEnvironment()
    environment.info = replace(
        ActivityEnvironment.default_info(), activity_id=activity_id, workflow_run_id=execution, attempt=1
    )
    return environment


def _a_turn(step: str, *, index: int, item_key: str, continuation: ContinuationRef | None, **arguments: object) -> InlineStepCall:
    """One turn of one item's line, as the workflow schedules it."""
    return InlineStepCall(
        spec=DIGEST,
        step=step,
        arguments=arguments,
        identity=IdentityOnTheWire(
            node_id="digest_each",
            item_key=item_key,
            turn=TurnOnTheWire(work=step, index=index, turns=("triage", "draft"), each="item", tools=()),
        ),
        continuation=continuation,
    )


def _outcome(answered: object) -> InlineStepOutcome:
    """The answer a turn's activity gave, refusing anything that is not one."""
    assert isinstance(answered, InlineStepOutcome)
    return answered


def _line(execution: str, item_key: str) -> UUID:
    """The conversation the directly-driven deliveries address for one item."""
    return continuation_conversation(execution, "digest_each", item_key)


@pytest.mark.asyncio(loop_scope="module")
async def test_a_whole_map_runs_on_a_real_server_and_leaves_its_line_in_the_record(
    temporal_client: Client, module_record: RecordDatabase, tmp_path: Path
) -> None:
    """The end-to-end shape: two turns of one item across a real activity boundary, carried by the record.

    The workflow, the sandbox, the payload converter and the worker are all real, so what this
    proves is that a `ContinuationRef` survives the round trip in both directions and that the
    second turn's request was built from the first turn's stored messages -- not from anything this
    process remembered, which is what the empty `lines` says.
    """
    asked = Asked()
    active = ActiveRun()
    registry = await _an_environment(module_record, tmp_path, asked, active)
    queue = "test-turn-continuation"
    bind_registry(queue, registry, BOUNDED)

    async with Worker(
        temporal_client,
        task_queue=queue,
        workflows=[DurableWorkflowRun],
        activities=step_activities(registry),
        workflow_runner=sandbox_runner(),
    ):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=DIGEST, params={"items": [{"title": "quantum computing"}]}, workflow_id="digest-each-item"),
            id="run-turn-continuation",
            task_queue=queue,
        )
        result = await handle.result()

    assert result == ["answer for turn 1"]
    assert len(asked.calls) == 2, "one model call per turn, and no repeat of either"
    assert TRIAGE_INSTRUCTIONS in " ".join(asked.prompts_of(1)), "turn 2 was asked with turn 1's own prompt in its history"
    assert asked.instructions[0] == asked.instructions[1], "both turns ran under the identical system prompt"
    assert active.conversations.conversations == {}, "the line was never in this process at all"

    store = PostgresStepStore(PoolDatabase(module_record.pool))
    written = await store.list_runs()
    lines = {run.conversation_id for run in written}
    assert len(written) == 2, "one single-shot run per turn"
    assert len(lines) == 1, "and both of them under the item's one conversation"


async def test_a_second_worker_finishes_the_line_the_first_one_started(record_database: RecordDatabase, tmp_path: Path, execution: str) -> None:
    """Worker A completes turn 1, is gone, and worker B completes turn 2 off the record alone.

    Worker B is a whole second environment: its own `ActiveRun`, its own model, its own ledger
    instance. It holds no memory of turn 1 and needs none.
    """
    asked_a, asked_b = Asked(), Asked()
    worker_a = await _an_environment(record_database, tmp_path / "a", asked_a, ActiveRun())
    first = _outcome(
        await _a_delivery(execution, activity_id="1").run(
            _brought_activity(worker_a),
            _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "quantum computing"}),
        )
    )
    del worker_a

    active_b = ActiveRun()
    worker_b = await _an_environment(record_database, tmp_path / "b", asked_b, active_b)
    second = _outcome(
        await _a_delivery(execution, activity_id="2").run(
            _brought_activity(worker_b),
            _a_turn("draft", index=1, item_key="0", continuation=first.continuation, triage=first.output),
        )
    )

    assert first.continuation is not None
    assert second.continuation is not None
    assert first.continuation.revision == 1
    assert second.continuation.revision == 2, "the line moved one revision on, on a worker that never saw turn 1"
    assert first.continuation.conversation_id == second.continuation.conversation_id == _line(execution, "0")
    assert len(asked_b.calls) == 1
    assert TRIAGE_INSTRUCTIONS in " ".join(asked_b.prompts_of(0)), "worker B was asked with turn 1's own prompt"
    assert asked_b.instructions == [asked_a.instructions[0]], "and under the system prompt worker A opened the line with"
    assert active_b.conversations.conversations == {}


async def test_a_crash_after_the_commit_hands_the_stored_answer_back_without_a_second_model_call(
    record_database: RecordDatabase, tmp_path: Path, execution: str
) -> None:
    """The activity committed and then died before Temporal heard. The redelivery must not run the turn again."""
    asked = Asked()
    registry = await _an_environment(record_database, tmp_path, asked, ActiveRun())
    activity = _brought_activity(registry)
    call = _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "quantum computing"})

    committed = _outcome(await _a_delivery(execution, activity_id="7").run(activity, call))
    redelivered = _outcome(await _a_delivery(execution, activity_id="7").run(activity, call))

    assert len(asked.calls) == 1, "the redelivery called no model"
    assert redelivered.output == committed.output, "and answered with exactly the payload the first delivery stored"
    assert redelivered.continuation == committed.continuation


async def test_two_items_carry_two_lines_that_know_nothing_of_each_other(
    record_database: RecordDatabase, tmp_path: Path, execution: str
) -> None:
    """A map fans out: each item's line advances on its own revision, and neither can settle the other's turn."""
    asked = Asked()
    registry = await _an_environment(record_database, tmp_path, asked, ActiveRun())
    activity = _brought_activity(registry)

    first, second = await asyncio.gather(
        _a_delivery(execution, activity_id="10").run(
            activity, _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "one"})
        ),
        _a_delivery(execution, activity_id="11").run(
            activity, _a_turn("triage", index=0, item_key="1", continuation=None, item={"title": "two"})
        ),
    )
    carried_on = _outcome(
        await _a_delivery(execution, activity_id="12").run(
            activity, _a_turn("draft", index=1, item_key="0", continuation=_outcome(first).continuation, triage="a note")
        )
    )

    one, other = _outcome(first).continuation, _outcome(second).continuation
    assert one is not None
    assert other is not None
    assert one.conversation_id == _line(execution, "0")
    assert other.conversation_id == _line(execution, "1")
    assert other.revision == 1, "item 1's line did not move because item 0's did"
    assert carried_on.continuation is not None
    assert carried_on.continuation.revision == 2
    assert carried_on.continuation.conversation_id == _line(execution, "0")


async def test_an_unfinished_turn_on_the_line_blocks_the_next_one(record_database: RecordDatabase, tmp_path: Path, execution: str) -> None:
    """A turn that began and never settled may have done its work. Nothing else runs on that line until it is settled."""
    asked = Asked()
    registry = await _an_environment(record_database, tmp_path, asked, ActiveRun())
    activity = _brought_activity(registry)
    checkpoints = PostgresContinuationStore(PoolDatabase(record_database.pool))
    await checkpoints.begin_attempt(
        _a_claim_on(_line(execution, "0"), attempt_id="c" * 64),
    )

    with pytest.raises(ApplicationError) as refusal:
        await _a_delivery(execution, activity_id="20").run(
            activity, _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "one"})
        )

    assert refusal.value.type == REFUSED_CONTINUATION
    assert refusal.value.non_retryable is True, "a redelivery would meet the same unfinished attempt"
    assert "c" * 64 in str(refusal.value), "the attempt holding the line is named"
    assert asked.calls == [], "the refusal came before any model call"


async def test_a_redelivery_of_a_turn_that_never_settled_is_refused_rather_than_replayed(
    record_database: RecordDatabase, tmp_path: Path, execution: str
) -> None:
    """Its own earlier attempt is unfinished, so whether it already acted is unknown and it does not act again."""
    asked = Asked()
    registry = await _an_environment(record_database, tmp_path, asked, ActiveRun())
    activity = _brought_activity(registry)
    call = _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "one"})
    claimed = await _the_attempt_of(record_database, call, execution=execution, activity_id="30")

    with pytest.raises(ApplicationError) as refusal:
        await _a_delivery(execution, activity_id="30").run(activity, call)

    assert refusal.value.type == REFUSED_CONTINUATION
    assert refusal.value.non_retryable is True
    assert claimed in str(refusal.value)
    assert "exactly once" not in str(refusal.value), "the refusal reports uncertainty, it does not claim a guarantee"
    assert asked.calls == []


async def test_a_tool_call_still_in_flight_refuses_the_commit_and_says_why(
    record_database: RecordDatabase, tmp_path: Path, execution: str
) -> None:
    """An effect nobody can confirm is not a settled turn. The refusal names the call rather than promising anything."""
    asked = Asked()
    registry = await _an_environment(record_database, tmp_path, asked, ActiveRun())
    activity = _brought_activity(registry)
    first = _outcome(
        await _a_delivery(execution, activity_id="40").run(
            activity, _a_turn("triage", index=0, item_key="0", continuation=None, item={"title": "one"})
        )
    )
    assert first.continuation is not None
    store = PostgresStepStore(PoolDatabase(record_database.pool))
    await store.record_tool_effect(
        ToolEffectRecord(tool_call_id="call_9", tool_name="write_note", run_id=first.continuation.run_id, status="started")
    )

    with pytest.raises(ApplicationError) as refusal:
        await _a_delivery(execution, activity_id="41").run(
            activity, _a_turn("draft", index=1, item_key="0", continuation=first.continuation, triage=first.output)
        )

    assert refusal.value.type == REFUSED_CONTINUATION
    assert "write_note/call_9" in str(refusal.value)
    assert "exactly once" not in str(refusal.value)


def _a_claim_on(conversation_id: UUID, *, attempt_id: str) -> ContinuationAttempt:
    """One claim on a line, with the deterministic run id the turn that made it would register."""
    return ContinuationAttempt(
        conversation_id=conversation_id, attempt_id=attempt_id, expected_revision=0, run_id=f"{conversation_id}:{attempt_id[:16]}"
    )


async def _the_attempt_of(record: RecordDatabase, call: InlineStepCall, *, execution: str, activity_id: str) -> str:
    """Claim the very attempt one delivery would claim, and answer with its id, leaving it unfinished."""
    assert call.identity is not None
    assert call.identity.turn is not None
    attempt_id = continuation_attempt_id(
        workflow_execution=execution,
        scheduled_activity_id=activity_id,
        node_id=call.identity.node_id,
        item_key=call.identity.item_key or "",
        turn_index=call.identity.turn.index,
        logical_retry=0,
    )
    checkpoints = PostgresContinuationStore(PoolDatabase(record.pool))
    await checkpoints.begin_attempt(_a_claim_on(_line(execution, call.identity.item_key or ""), attempt_id=attempt_id))
    return attempt_id


@dataclass(frozen=True)
class Sales:
    """A step's declared argument type: not a primitive, so a dict reaching the step would be visible."""

    quarter: str
    total: int


@dataclass(frozen=True)
class Draft:
    text: str


REROUTED: list[str] = []
"""`<activity name>|<argument type>:<value>` for every call `draft_report` received."""

REFUSED: list[str] = []
"""One entry per turn the refusing registry was asked to perform."""


async def fetch_sales(quarter: str) -> Sales:
    return Sales(quarter=quarter, total=1000)


async def draft_report(sales: Sales) -> Draft:
    """Record which activity delivered this call and what `sales` arrived as."""
    REROUTED.append(f"{activity.info().activity_type}|{type(sales).__name__}:{sales.total}")
    return Draft(text=f"net {sales.total}")


REPAIRED = Workflow(
    name="report-with-a-repair",
    version=1,
    description="Draft a report from typed sales, have a person refuse it once, and draft it again.",
    parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
    nodes=(
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="draft_report", work="draft_report", inputs={"sales": FromNode(node="fetch_sales")}),
        PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
    ),
    output=FromNode(node="approve"),
)


@pytest.fixture(autouse=True)
def _fresh_calls() -> Iterator[None]:
    REROUTED.clear()
    REFUSED.clear()
    yield
    REROUTED.clear()
    REFUSED.clear()


def _published_registry() -> StepRegistry:
    """Two ordinary Python steps, both published as activities of their own name."""
    registry = StepRegistry()
    registry.register(fetch_sales)
    registry.register(draft_report)
    return registry


def _refusing_registry() -> StepRegistry:
    """A registry whose every turn meets a refusal from its line, the way a wedged conversation does."""

    async def refuse(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, output_type, inputs
        REFUSED.append(activity.info().activity_id)
        msg = "attempt 'aaaa' of conversation 0f8c1d2e-1111-4222-8333-444455556666 is still running under run 'run-elsewhere'"
        raise ContinuationInFlightError(msg)

    return StepRegistry(perform=refuse)


def _a_worker(client: Client, queue: str, registry: StepRegistry) -> Worker:
    """A worker on its own queue, scheduling every step under `BOUNDED` and nothing else."""
    bind_registry(queue, registry, BOUNDED)
    return Worker(
        client,
        task_queue=queue,
        workflows=[DurableWorkflowRun],
        activities=step_activities(registry),
        workflow_runner=sandbox_runner(),
    )


async def _wait_until_waiting(handle: WorkflowHandle[DurableWorkflowRun, object], gate_id: str) -> None:
    for _ in range(400):
        waiting = await handle.query(DurableWorkflowRun.waiting_at)
        if waiting is not None and waiting.gate_id == gate_id:
            return
        await asyncio.sleep(0.05)
    msg = f"run never waiting at {gate_id!r}"
    raise AssertionError(msg)


@pytest.mark.asyncio(loop_scope="module")
async def test_a_published_step_owing_a_repair_keeps_its_declared_types_through_the_reroute(temporal_client: Client) -> None:
    """The one path where a published step's arguments cross as a plain JSON document, on a real server.

    A step the worker published is normally scheduled by its own name, with its arguments typed
    against its own activity signature. A call that owes a repair cannot go that way -- the step's
    activity has nowhere to carry the rejection -- so it travels whole through
    `PERFORM_INLINE_STEP`, where every argument is one entry in a `dict[str, object]` and the
    activity re-validates it against the rebuilt step's own annotation.

    Both facts are asserted: the second call arrived through the generic activity, and `sales`
    reached the step as a `Sales` rather than as the dict it was serialised to.
    """
    queue = "test-published-reroute"
    async with _a_worker(temporal_client, queue, _published_registry()):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=REPAIRED, params={"quarter": "Q2"}, workflow_id="report-with-a-repair"),
            id="run-published-reroute",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        await handle.signal(
            DurableWorkflowRun.decide,
            GateVerdict(gate_id="approve", approved=False, feedback="no evidence", requested_repair="cite the quarter"),
        )
        for _ in range(400):
            if len(REROUTED) == 2:
                break
            await asyncio.sleep(0.05)
        await _wait_until_waiting(handle, "approve")
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        result = await handle.result()

    assert result == {"text": "net 1000"}
    delivered_by, then_by = REROUTED
    assert delivered_by == f"draft_report|{Sales.__name__}:1000", "the first call went to the step's own activity"
    assert then_by == f"{PERFORM_INLINE_STEP}|{Sales.__name__}:1000", (
        "the repair went through the generic activity, and its argument still arrived as the type the step declared"
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_a_refused_continuation_ends_the_run_instead_of_retrying_forever(temporal_client: Client) -> None:
    """A wedged line is a final failure the history records, not an activity Temporal keeps re-delivering.

    The worker schedules under `BOUNDED`, which carries no retry policy, so Temporal's own unbounded
    default applies: a retryable failure here would re-deliver the same attempt forever and this test
    would never end. It ends, once, with the refusal in the run's own history.
    """
    queue = "test-refused-continuation"
    async with _a_worker(temporal_client, queue, _refusing_registry()):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=DIGEST, params={"items": [{"title": "quantum computing"}]}, workflow_id="digest-each-item"),
            id="run-refused-continuation",
            task_queue=queue,
        )
        with pytest.raises(WorkflowFailureError) as failure:
            await handle.result()

    assert len(REFUSED) == 1, "one delivery, not an endless stream of them"
    assert REFUSED_CONTINUATION in _failure_types(failure.value), "the run's history names what ended it"
    assert "is still running under run 'run-elsewhere'" in _reported(failure.value), "the refusal's own words reach that history"


def _reported(failure: BaseException) -> str:
    """Every message in a failure's causal chain, joined -- what a reader of the run's history sees."""
    reported: list[str] = []
    current: BaseException | None = failure
    while current is not None:
        reported.append(str(current))
        current = current.__cause__
    return " | ".join(reported)


def _failure_types(failure: BaseException) -> list[str]:
    """Every `ApplicationError` type in a failure's causal chain, so a reader can name what ended the run."""
    types: list[str] = []
    current: BaseException | None = failure
    while current is not None:
        if isinstance(current, ApplicationError) and current.type is not None:
            types.append(current.type)
        current = current.__cause__
    return types


@pytest.mark.asyncio(loop_scope="module")
async def test_the_decide_act_carries_a_persons_words_to_the_durable_gate(
    temporal_client: Client, temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A refusal typed in the browser reaches the rerun as the words that were typed.

    Driven through `run_decide`, the very body the route and the console both run, rather than
    through a hand-built signal: the signal is what every other test here sends, and it is exactly
    the step that was dropping the words.
    """
    queue = "test-decide-feedback"
    session = a_runtime(state=a_state(tmp_path))
    session.active.workflow_id = "run-decide-feedback"
    on_temporal = RunSettings(
        run_engine="temporal",
        temporal_address=temporal_env.client.service_client.config.target_host,
        temporal_namespace=temporal_env.client.namespace,
    )
    async with _a_worker(temporal_client, queue, _published_registry()):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=REPAIRED, params={"quarter": "Q2"}, workflow_id="report-with-a-repair"),
            id="run-decide-feedback",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        session.active.pending = _waiting_at("approve", session.active.workflow_id)
        refusal = await run_decide(session, Decision(approve=False, feedback="cite the quarter"), on_temporal)
        assert refusal.status_code == 200
        for _ in range(400):
            if len(REROUTED) == 2:
                break
            await asyncio.sleep(0.05)
        evidence = await handle.query(DurableWorkflowRun.evidence)
        await _wait_until_waiting(handle, "approve")
        session.active.pending = _waiting_at("approve", session.active.workflow_id)
        assert (await run_decide(session, Decision(approve=True), on_temporal)).status_code == 200
        await handle.result()

    owed = evidence.retry_feedback["draft_report"]
    assert [item.feedback for item in owed] == ["cite the quarter"], "the rerun was told what was wrong, in the reviewer's words"
    assert [item.requested_repair for item in owed] == ["cite the quarter"], "and what to change, falling back to the same words"


def _waiting_at(gate_id: str, workflow_id: str) -> PendingReview:
    """The gate a session shows as waiting, as the durable mirror would have set it."""
    return PendingReview(gate_id=gate_id, workflow=workflow_id, reviewing="", rejection_reruns="draft_report")
