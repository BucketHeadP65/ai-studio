"""Durable execution on Temporal: the built graph runs *as* the workflow, and its steps run as activities.

A step marked `replay_safe` runs in workflow code instead, and `ActivityExecutor` makes that choice.
An inline step has no activity of its own, so every worker also publishes `PERFORM_INLINE_STEP`,
which rebuilds the step from the workflow it is handed and runs it. The core's three ports become
Temporal parts: `StepExecutor` is `workflow.execute_activity`, `ApprovalHook` is a signal plus
`workflow.wait_condition` so a gate outlives any worker, and `RunObserver` is queryable state.
Nothing here reads a clock other than `workflow.now()`, generates randomness, or does I/O. All of
that lives in activities.
"""

from __future__ import annotations

import functools
import inspect

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid5

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from temporalio.workflow import ActivityConfig

# Passed through the sandbox, so workflow code reaches the very objects the worker registered,
# not sandboxed copies of their modules.
with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
    from pydantic_ai.durable_exec.temporal import AgentPlugin, LogfirePlugin, PydanticAIPlugin

    from core.ai.history.contracts import (
        ContinuationAttempt,
        ContinuationConflictError,
        ContinuationInFlightError,
        ContinuationPayloadTooLargeError,
        ContinuationRef,
        ContinuationUnsafeReplayError,
    )
    from core.ai.workflows.birth import shapes_of
    from core.ai.workflows.domain.evidence import approved_gates
    from core.ai.workflows.domain.identity import (
        CURRENT_STEP,
        ReviewedArtifacts,
        StepIdentity,
        TurnFacts,
        canonical_reviewed_value,
        performing_step,
    )
    from core.ai.workflows.domain.parameter_values import WorkflowArguments
    from core.ai.workflows.domain.run_events import GateDecisionFact, RetryFeedback
    from core.ai.workflows.domain.vocabulary import PersonGate, Tier, Workflow
    from core.ai.workflows.exceptions import RetryExhaustedError
    from core.ai.workflows.graph_builder import GateDecision, WorkflowState, build_graph

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
    from typing import Never

    from pydantic_ai.agent.abstract import AbstractAgent
    from temporalio.client import Plugin

    from core.ai.workflows.registry import StepImplementation, StepRegistry

DEFAULT_TASK_QUEUE = "pydantic-ai-workflows"
"""The queue a worker serves and a run is started on when nobody says otherwise."""

WORKFLOW_TYPE = "PydanticAIWorkflowRun"
"""The registered Temporal workflow type. It must not change while a run is in flight."""

EXHAUSTED_GATE = "RetryExhaustedError"
"""The failure type a client reads to tell a spent gate ceiling from any other run failure.

It must not change while a run is in flight."""

PERFORM_INLINE_STEP = "core.ai.workflows.perform_brought_step"
"""The activity every worker publishes for a workflow's inline steps.

Reserved: a step registered under this name would be shadowed by it. No step named after its own
function can take it, since a Python identifier holds no dots. It must not change while a run is
in flight.
"""

REFUSED_CONTINUATION = "ContinuationRefused"
"""The failure type a client reads to tell a refused durable turn from any other run failure.

It must not change while a run is in flight."""

WORKFLOW_NAMESPACE: Final[UUID] = UUID("89cf71dc-4d4e-520f-96c5-01cb2f57df20")
"""The namespace every durable step conversation's id is derived in.

It must never change: a new namespace gives every step conversation in flight a new identity and
orphans the checkpoints it already committed."""


def continuation_conversation(workflow_run_id: str, node_id: str, item_key: str) -> UUID:
    """The step conversation one durable run keeps for one node and one item.

    The same three values always give the same id, so two workers in different processes address
    the same conversation without asking anything.

    Args:
        workflow_run_id: The Temporal workflow execution's run id.
        node_id: The node the step belongs to.
        item_key: The item the node is working on, empty for a node over no items.

    Returns:
        The conversation id.
    """
    return uuid5(WORKFLOW_NAMESPACE, f"{workflow_run_id}:{node_id}:{item_key}")


def continuation_attempt_id(  # noqa: PLR0913 (the six values are the identity itself)
    *, workflow_execution: str, scheduled_activity_id: str, node_id: str, item_key: str, turn_index: int, logical_retry: int
) -> str:
    """The id one logical attempt at one turn is known by.

    Temporal's delivery attempt is left out: a retried delivery of the same scheduled activity is
    the same logical attempt and addresses the same stored row. What the workflow schedules again,
    such as a rewind or a fresh activity, carries a new scheduled id and, when a rejection was
    answered, a higher `logical_retry`, so it is a new attempt.

    Args:
        workflow_execution: The Temporal workflow execution's run id.
        scheduled_activity_id: The id Temporal gave the scheduled activity.
        node_id: The node the step belongs to.
        item_key: The item the node is working on.
        turn_index: Which turn of the step conversation this is.
        logical_retry: How many rejections this call owes a repair for.

    Returns:
        The SHA-256 hex digest of the six values.
    """
    return sha256(
        f"{workflow_execution}:{scheduled_activity_id}:{node_id}:{item_key}:{turn_index}:{logical_retry}".encode()
    ).hexdigest()


def continuation_run_id(conversation_id: UUID, attempt_id: str) -> str:
    """The run id one attempt registers with the record.

    It is derived from the attempt, so the attempt row can hold it before the run is registered and
    a commit can join the run once it is.

    Args:
        conversation_id: The step conversation's id.
        attempt_id: The attempt's id.

    Returns:
        The conversation id and the first sixteen characters of the attempt id.
    """
    return f"{conversation_id}:{attempt_id[:16]}"


@dataclass
class StepConversationContinuation:
    """Where one durable turn was told to resume, and where it left its step conversation.

    `given` is the checkpoint the workflow knows of, absent on the conversation's first turn.
    `committed` is what the turn settled on, read back by the activity and carried to the next turn.
    """

    attempt: ContinuationAttempt
    given: ContinuationRef | None
    committed: ContinuationRef | None = None


CONTINUING_STEP_CONVERSATION: ContextVar[StepConversationContinuation | None] = ContextVar("continuing_step_conversation", default=None)
"""The step conversation the step now running is one turn of, or None when it continues none.

Published around the step, as `CURRENT_STEP` is. The host's step body reads it to load the
checkpoint and write the next one."""


@contextmanager
def continuing_step_conversation(state: StepConversationContinuation) -> Generator[StepConversationContinuation]:
    """Publish `state` as the step conversation the enclosed step continues, and take it back afterwards.

    Args:
        state: The continuation to publish.

    Yields:
        `state`, published for the duration of the block.
    """
    token = CONTINUING_STEP_CONVERSATION.set(state)
    try:
        yield state
    finally:
        CONTINUING_STEP_CONVERSATION.reset(token)


_DEFAULT_ACTIVITY_CONFIG = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=RetryPolicy(maximum_attempts=5, non_retryable_error_types=("NotFoundError", "TypeError", "ValidationError")),
)
"""How a step is scheduled when the caller states nothing: five minutes per attempt and at most five attempts.

An activity the worker never registered (`NotFoundError`) and a step that raises `TypeError` or
pydantic's `ValidationError` fail the run on the first attempt. An inline step validates its
arguments inside the activity, so an argument that does not fit it fails there. A published
step's arguments are decoded before the step runs, and one that does not decode is retried up to
the ceiling."""


class GateVerdict(BaseModel):
    """One person's answer to one gate. The payload of the `decide` signal."""

    gate_id: str
    approved: bool
    feedback: str = ""
    """What was wrong with what was reviewed. Read on a refusal, ignored on an approval."""
    requested_repair: str = ""
    """What to change. Empty leaves `feedback` as the whole instruction."""


class WaitingGate(BaseModel):
    """What a waiting run is waiting for, as a reader of the `waiting_at` query sees it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    gate_id: str
    reviewing: object
    """The value being reviewed, carried by the payload converter as its own type."""
    rejection_reruns: str
    """The node a rejection runs again, shown to whoever decides."""
    note: str = ""
    """What a judge said when it could not decide, shown ahead of the value. Blank for a person gate."""


class StepTransition(BaseModel):
    """One node starting or ending, as the `progress` query reports it."""

    seq: int
    node: str
    at: float
    """Seconds since the run began, measured on `workflow.now()`, so a replay gives the same value."""
    finished: bool
    failed: bool


class RunProgress(BaseModel):
    """Where a durable run is, read off the workflow rather than out of a library."""

    running: list[str]
    done: list[str]
    failed: list[str]
    transitions: list[StepTransition]
    waiting: WaitingGate | None


class RunEvidence(BaseModel):
    """What a finished durable run has evidence of, derived inside the workflow.

    The shapes in `produced` come from the typed values, so a dataclass is named as itself, never
    as a dict.
    """

    gates_approved: list[str]
    gates_rejected: dict[str, int] = Field(default_factory=dict)
    """Refusals heard before this run ended, by gate id."""
    gates_auto_approved: list[str] = Field(default_factory=list)
    """Gates this run passed automatically, with nobody asked. They are never in `gates_approved`,
    so an automatic pass never counts as a person's approval."""
    produced: dict[str, str]
    gate_decisions: list[GateDecisionFact] = Field(default_factory=list)
    """Every decision every gate received, in order, each naming its reviewer and the generation
    of the value it was about."""
    retry_feedback: dict[str, list[RetryFeedback]] = Field(default_factory=dict)
    """The rejections each node owes a repair for, by node id, each with the refused value."""


class RunRequest(BaseModel):
    """What starting a durable run takes. The workflow travels as data."""

    spec: Workflow
    params: WorkflowArguments
    """The arguments this run was started with, as the JSON each parameter declares."""
    workflow_id: str
    """Which saved workflow this run is of, so its history names it."""
    run_title: str = ""
    """What this run itself is called, in the words of whoever started it. Empty for a run nobody named."""
    gate_mode: Literal["wait", "auto"] = "wait"
    """What a person gate does: `wait` holds it for a signal, `auto` passes it with nobody asked. Travels
    with the request, so a history says what this run was allowed to decide on its own."""


class TurnOnTheWire(BaseModel):
    """`TurnFacts` without `outputs`: a type is not data, so it does not cross the activity boundary.

    The activity rebuilds `outputs` from its own extended registry, resolving each name in
    `turns` against the registry it built from the very workflow this call carries.
    """

    work: str
    index: int
    turns: tuple[str, ...]
    each: str
    tools: tuple[str, ...]
    skills: tuple[str, ...] = ()
    """Every skill the turn sequence holds, empty when it holds none."""


class IdentityOnTheWire(BaseModel):
    """`StepIdentity` as it crosses the activity boundary, with `turn` carried as `TurnOnTheWire`."""

    node_id: str
    item_key: str | None = None
    turn: TurnOnTheWire | None = None
    tier: Tier | None = None
    retry_feedback: tuple[RetryFeedback, ...] = ()
    parameters_json: str = "{}"
    reviewed_values: tuple[str, ...] = ()


class InlineStepCall(BaseModel):
    """What performing one inline step takes: the workflow that declares it, its name and its arguments.

    Every call carries the whole workflow, because the worker holds the app's steps and authoring
    terms and only the workflow holds the inline step. The arguments cross as one JSON document,
    and each is decoded against the rebuilt step's own annotation, so the step receives the types
    it declared. `identity` is whatever `CURRENT_STEP` carried when the workflow scheduled this
    call. The activity publishes it again around the step, so a step reading `CURRENT_STEP` sees
    what it would see in-process.
    """

    spec: Workflow
    step: str
    arguments: dict[str, object]
    identity: IdentityOnTheWire | None = None
    continuation: ContinuationRef | None = None
    """Where the step conversation this call is a turn of stands, or None on its first turn and on a
    call that continues none. It holds an address and a digest, never a transcript: the messages stay
    in the record, and the activity reads exactly the step this names."""


class InlineStepOutcome(BaseModel):
    """What one turn of a durable step conversation answers with: its output as JSON, and where it left the conversation.

    A call that continues no step conversation answers with the step's own output instead, typed
    against the step's own annotation. The identity the workflow scheduled the call with decides
    which shape it gets, so both sides read one rule.

    Let every run in flight finish before changing whether a step's calls continue a step
    conversation. A history recording one shape, replayed by a worker that now answers the other,
    diverges, as it does when `replay_safe` flips.
    """

    output: JsonValue
    continuation: ContinuationRef | None = None


@dataclass(frozen=True)
class WorkerBinding:
    """What a worker lends the workflows it serves: its steps, how they are scheduled, and which of them it published."""

    registry: StepRegistry
    activity_config: ActivityConfig
    published: frozenset[str]
    """Step names with an activity of their own on this worker. Any other step is performed
    through `PERFORM_INLINE_STEP`."""


_BINDINGS: dict[str, WorkerBinding] = {}
"""What the worker serving each task queue can execute, by task queue, for its workflows to read back."""


def bind(task_queue: str, registry: StepRegistry, activity_config: ActivityConfig | None = None) -> None:
    """Publish what the worker on `task_queue` can execute. Call it before the worker starts polling.

    Args:
        task_queue: The queue the worker serves.
        registry: The steps the worker can execute.
        activity_config: How each step activity is scheduled. Five minutes per attempt and at
            most five attempts when not given.
    """
    _BINDINGS[task_queue] = WorkerBinding(
        registry=registry,
        activity_config=activity_config or _DEFAULT_ACTIVITY_CONFIG,
        published=_published_names(registry),
    )


def _binding(task_queue: str) -> WorkerBinding:
    binding = _BINDINGS.get(task_queue)
    if binding is None:
        msg = f"no registry is bound to task queue {task_queue!r}. Build the worker with `worker()` so its steps are published"
        raise RuntimeError(msg)
    return binding


def _positional(impl: StepImplementation, kwargs: Mapping[str, object]) -> list[object]:
    """The step's arguments in declared order, each unwired parameter filled from its default."""
    args: list[object] = []
    for name, parameter in inspect.signature(impl.function).parameters.items():
        if name in kwargs:
            args.append(kwargs[name])
        elif parameter.default is not inspect.Parameter.empty:
            args.append(parameter.default)
        else:
            msg = f"step {impl.name!r} needs {name!r}, which the spec did not wire. Validation should have refused this"
            raise RuntimeError(msg)
    return args


@dataclass(frozen=True)
class ScheduledWork:
    """Which activity performs one step, the arguments it is scheduled with, and the step conversation it continues."""

    activity: str
    args: list[object]
    conversation: tuple[str, str] | None = None
    """The node and item whose conversation this call is one turn of, or None when it continues none.
    It decides the shape of the answer: a turn of a step conversation answers with an `InlineStepOutcome`."""


def step_conversation_of(identity: StepIdentity | None) -> tuple[str, str] | None:
    """Which step conversation a step now performing is one turn of, or None when it is one-shot.

    The workflow scheduling the call and the activity answering it both read this one rule, so they
    agree on the shape on the wire.

    Args:
        identity: What `CURRENT_STEP` holds for the step, or None.

    Returns:
        The node id and the item key, or None.
    """
    if identity is None or identity.turn is None:
        return None
    return (identity.node_id, identity.item_key or "")


def _identity_on_wire(identity: StepIdentity | None) -> IdentityOnTheWire | None:
    """`CURRENT_STEP`'s value as `InlineStepCall.identity` carries it, or `None` when nothing was published."""
    if identity is None:
        return None
    turn = (
        TurnOnTheWire(work=identity.turn.work, index=identity.turn.index, turns=identity.turn.turns, each=identity.turn.each,
                       tools=identity.turn.tools, skills=identity.turn.skills)
        if identity.turn is not None
        else None
    )
    values: tuple[str, ...] = ()
    if identity.retry_feedback:
        reader = identity.reviewed_artifacts
        if reader is None:
            msg = f"the repair owed by {identity.node_id!r} carries no reader for the values it was refused over"
            raise ValueError(msg)
        # Resolved here in workflow code. The activity runs in another process, where this run's values do not exist.
        values = tuple(canonical_reviewed_value(reader.read(item.reviewed)) for item in identity.retry_feedback)
    return IdentityOnTheWire(node_id=identity.node_id, item_key=identity.item_key, turn=turn, tier=identity.tier,
                             retry_feedback=identity.retry_feedback, parameters_json=identity.parameters_json, reviewed_values=values)


def _artifacts_from_wire(wire: IdentityOnTheWire) -> ReviewedArtifacts:
    """The reviewed values a call carried, each checked against its digest, failing non-retryably on a mismatch."""
    artifacts = ReviewedArtifacts(run_id=wire.retry_feedback[0].reviewed.run_id if wire.retry_feedback else "")
    try:
        for feedback, canonical in zip(wire.retry_feedback, wire.reviewed_values, strict=True):
            artifacts.values[feedback.reviewed] = canonical
            artifacts.read(feedback.reviewed)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    return artifacts


def _identity_from_wire(wire: IdentityOnTheWire, extended: StepRegistry) -> StepIdentity:
    """Rebuild the identity a call carried, resolving each turn's output type on this worker's extended registry."""
    artifacts = _artifacts_from_wire(wire)
    if wire.turn is None:
        return StepIdentity(node_id=wire.node_id, item_key=wire.item_key, tier=wire.tier,
                            retry_feedback=wire.retry_feedback, parameters_json=wire.parameters_json, reviewed_artifacts=artifacts)
    outputs: list[type] = []
    for name in wire.turn.turns:
        impl = extended.get(name)
        if impl is None:
            msg = f"this worker cannot rebuild turn {name!r} of {wire.node_id!r} from the spec it was handed"
            raise ApplicationError(msg, non_retryable=True)
        outputs.append(impl.output_type)
    turn = TurnFacts(
        work=wire.turn.work, index=wire.turn.index, turns=wire.turn.turns, each=wire.turn.each, tools=wire.turn.tools,
        outputs=tuple(outputs), skills=wire.turn.skills,
    )
    return StepIdentity(node_id=wire.node_id, item_key=wire.item_key, turn=turn, tier=wire.tier,
                        retry_feedback=wire.retry_feedback, parameters_json=wire.parameters_json, reviewed_artifacts=artifacts)


@dataclass(frozen=True)
class ActivityExecutor:
    """A `StepExecutor` that schedules each step as a Temporal activity, unless the step is `replay_safe`.

    The activity is named after the step, not the node, so a worker's activity table is its registry.

    **A `replay_safe` step is awaited in workflow code instead**, so its retry unit becomes the
    model request and its body runs again on every replay. Let every run in flight finish before
    flipping a step's `replay_safe`: a history recording a scheduled activity, replayed by a worker
    that now awaits the step inline, diverges and fails the workflow task.

    **A step the worker did not publish, which is every inline step, goes through
    `PERFORM_INLINE_STEP`.** **So does a call that owes a repair**, published or not, because a
    step's own activity is typed on its arguments alone and cannot carry the rejection it answers.
    """

    workflow: Workflow
    """The workflow this run is of, sent with every inline step call so the activity can rebuild the step."""
    published: frozenset[str]
    """Step names the worker registered as activities of their own, as `WorkerBinding.published` holds them."""
    activity_config: ActivityConfig
    conversations: dict[tuple[str, str], ContinuationRef] = field(default_factory=dict)
    """Where each step conversation of this run stands, by node and item. Filled only from activity
    results, so a replay rebuilds it from the history."""

    def scheduling(self, impl: StepImplementation, kwargs: Mapping[str, object]) -> ScheduledWork:
        """Which activity performs this step: the one named after it, or the one that rebuilds it.

        A published step is scheduled by its own name with its arguments in declared order, since
        Temporal types each against that step's activity signature. Any other step, an inline one or
        any step owing a repair, travels whole in one `InlineStepCall` with the identity
        `CURRENT_STEP` published, so the activity can publish the same one again, rejections and
        reviewed values included.

        Args:
            impl: The step to perform.
            kwargs: Its resolved arguments by parameter name.

        Returns:
            The activity, its arguments, and the step conversation the call continues.
        """
        identity = CURRENT_STEP.get()
        owes_a_repair = identity is not None and bool(identity.retry_feedback)
        if impl.name in self.published and not owes_a_repair:
            return ScheduledWork(activity=impl.name, args=_positional(impl, kwargs))
        conversation = step_conversation_of(identity)
        call = InlineStepCall(
            spec=self.workflow,
            step=impl.name,
            arguments=dict(kwargs),
            identity=_identity_on_wire(identity),
            continuation=self.conversations.get(conversation) if conversation is not None else None,
        )
        return ScheduledWork(activity=PERFORM_INLINE_STEP, args=[call], conversation=conversation)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        """Await a replay-safe step here, or schedule any other step and await its recorded result.

        A turn of a step conversation answers with an `InlineStepOutcome`, and the reference it
        committed is kept, so the next turn of the same conversation is scheduled against it. A
        replay rebuilds the same references, since they come from activity results in history order.

        Args:
            node_id: The node performing the step. Unused here.
            implementation: The step to perform.
            kwargs: Its resolved arguments by parameter name.

        Returns:
            What the step produced, as its output type.
        """
        del node_id
        if implementation.replay_safe:
            return await implementation.function(**kwargs)
        scheduled = self.scheduling(implementation, kwargs)
        if not self.continues(scheduled):
            return await workflow.execute_activity(
                scheduled.activity, args=scheduled.args, result_type=implementation.output_type, **self.activity_config
            )
        outcome = await workflow.execute_activity(
            scheduled.activity, args=scheduled.args, result_type=InlineStepOutcome, **self.activity_config
        )
        if scheduled.conversation is not None and outcome.continuation is not None:
            self.conversations[scheduled.conversation] = outcome.continuation
        return TypeAdapter(implementation.output_type).validate_python(outcome.output)

    def continues(self, scheduled: ScheduledWork) -> bool:
        """Whether this call's answer is an `InlineStepOutcome` rather than the step's own output.

        A call that is no turn of a step conversation never answers with one. A call that is one always does.

        Args:
            scheduled: The scheduled call.

        Returns:
            True when the answer is an `InlineStepOutcome`.
        """
        return scheduled.conversation is not None


def _exhausted_gate(exc: RetryExhaustedError) -> ApplicationError:
    """A spent gate ceiling as a final Temporal failure, with the whole rejection as its detail."""
    return ApplicationError(str(exc), exc.feedback.model_dump(mode="json"), type=EXHAUSTED_GATE, non_retryable=True)


@workflow.defn(name=WORKFLOW_TYPE)
class DurableWorkflowRun:
    """One run of one built workflow, as a Temporal workflow.

    The instance is also the run's `ApprovalHook` and its `RunObserver`, whose state a replay
    rebuilds from history.
    """

    def __init__(self) -> None:
        self._verdicts: dict[str, GateDecision] = {}
        self._refused: dict[str, int] = {}
        self._waiting: WaitingGate | None = None
        self._running: list[str] = []
        self._done: list[str] = []
        self._failed: list[str] = []
        self._transitions: list[StepTransition] = []
        self._began: float = 0.0
        self._gates: dict[str, str] = {}
        self._workflow: Workflow | None = None
        self._state = WorkflowState(params={})
        self._auto: list[str] = []
        self.gate_mode: Literal["wait", "auto"] = "wait"

    @workflow.run
    async def run(self, request: RunRequest) -> object:
        """Build the graph from the workflow and this worker's registry, then run it.

        Args:
            request: The workflow, its arguments and its gate mode.

        Returns:
            What the graph run returns.

        Raises:
            ApplicationError: When a gate's retry ceiling is spent, typed `EXHAUSTED_GATE` and final.
        """
        binding = _binding(workflow.info().task_queue)
        self._began = workflow.now().timestamp()
        self._workflow = request.spec
        self._gates = {node.id: node.rejected_to for node in request.spec.nodes if isinstance(node, PersonGate)}
        self._state = WorkflowState(params=dict(request.params), reviewed_artifacts=ReviewedArtifacts(run_id=workflow.info().workflow_id))
        self.gate_mode = request.gate_mode
        graph = build_graph(
            request.spec,
            binding.registry,
            approval=self,
            observer=self,
            executor=ActivityExecutor(workflow=request.spec, published=binding.published, activity_config=binding.activity_config),
        )
        try:
            return await graph.run(state=self._state)
        except RetryExhaustedError as exc:
            raise _exhausted_gate(exc) from exc

    @workflow.query
    def evidence(self) -> RunEvidence:
        """What this run did, for the caller's evidence log.

        Returns:
            The gates approved, rejected and passed automatically, the shapes produced, every gate
            decision and every rejection owed a repair.
        """
        if self._workflow is None:
            return RunEvidence(
                gates_approved=[], gates_auto_approved=list(self._auto), gates_rejected=dict(self._refused), produced={}
            )
        return RunEvidence(
            gates_approved=[gate for gate in approved_gates(self._workflow, self._state.values) if gate not in self._auto],
            gates_auto_approved=list(self._auto),
            gates_rejected=dict(self._refused),
            produced=shapes_of(self._state.values),
            gate_decisions=list(self._state.gate_decisions),
            retry_feedback=dict(self._state.retry_feedback),
        )

    @workflow.signal
    def decide(self, verdict: GateVerdict) -> None:
        """Take a person's verdict on a gate, as a signal that outlives any worker.

        Args:
            verdict: The gate id and the person's answer.
        """
        self._verdicts[verdict.gate_id] = GateDecision(
            approve=verdict.approved, feedback=verdict.feedback, requested_repair=verdict.requested_repair, source="person"
        )

    @workflow.query
    def waiting_at(self) -> WaitingGate | None:
        """What this run is waiting for, or None if it is not waiting.

        Returns:
            The waiting gate, or None.
        """
        return self._waiting

    @workflow.query
    def progress(self) -> RunProgress:
        """Every transition this run has made, and where it currently is.

        Returns:
            The running, finished and failed nodes, every transition, and the gate it waits at.
        """
        return RunProgress(
            running=list(self._running),
            done=list(self._done),
            failed=list(self._failed),
            transitions=list(self._transitions),
            waiting=self._waiting,
        )

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        """Wait until somebody signals a verdict for this gate, or pass it when gates pass automatically.

        The run waits here as long as it takes and survives a worker dying meanwhile, because the
        wait is part of the workflow's history. Under gate mode `auto` the gate never waits, and its
        id is recorded apart from the approvals so the evidence names what decided it.

        Args:
            gate_id: Which gate this is.
            value: What is being reviewed.
            note: What a judge said when it could not decide, carried onto `waiting_at` ahead of
                the value.

        Returns:
            The person's verdict, or an automatic approval.
        """
        if self.gate_mode == "auto":
            if gate_id not in self._auto:
                self._auto.append(gate_id)
            return GateDecision(approve=True, source="auto")
        self._waiting = WaitingGate(gate_id=gate_id, reviewing=value, rejection_reruns=self._gates.get(gate_id, "(unknown)"), note=note)
        await workflow.wait_condition(lambda: gate_id in self._verdicts)
        self._waiting = None
        # Popped rather than read: a rejection re-runs the gate's target and asks again, and a
        # verdict left behind would answer the second question with the first answer.
        verdict = self._verdicts.pop(gate_id)
        if not verdict.approve:
            # Refusals are evidence: a gate rejected and then approved must not read as waved through.
            self._refused[gate_id] = self._refused.get(gate_id, 0) + 1
        return verdict

    def started(self, node_id: str) -> None:
        """Record that a node began.

        Args:
            node_id: The node that began.
        """
        if node_id not in self._running:
            self._running.append(node_id)
        self._record(node_id, finished=False, failed=False)

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Record that a node ended, well or badly.

        Args:
            node_id: The node that ended.
            failed: Whether it failed.
        """
        if node_id in self._running:
            self._running.remove(node_id)
        if failed and node_id not in self._failed:
            self._failed.append(node_id)
        if node_id in self._done:
            self._done.remove(node_id)  # a rejected gate runs its upstream again, so keep the last say
        self._done.append(node_id)
        self._record(node_id, finished=True, failed=failed)

    def _record(self, node_id: str, *, finished: bool, failed: bool) -> None:
        at = round(workflow.now().timestamp() - self._began, 3)
        self._transitions.append(StepTransition(seq=len(self._transitions) + 1, node=node_id, at=at, finished=finished, failed=failed))


def _published_names(registry: StepRegistry) -> frozenset[str]:
    """The steps that get an activity of their own: every registered step but a replay-safe one."""
    return frozenset(name for name in registry.names if (impl := registry.get(name)) is not None and not impl.replay_safe)


def step_activities(registry: StepRegistry) -> list[Callable[..., Awaitable[object]]]:
    """One Temporal activity per registered step, named after the step, plus `PERFORM_INLINE_STEP`.

    Each step is wrapped, not decorated in place, so the caller's callables are never changed, and
    the wrapper keeps the signature the payload converter types against. A `replay_safe` step gets
    no activity, so reaching one as an activity fails as unregistered. `PERFORM_INLINE_STEP` is
    always published, whatever the registry's authoring terms allow, since it is the only activity
    that can run an inline step.

    Args:
        registry: The steps to publish.

    Returns:
        The activities, `PERFORM_INLINE_STEP` first and then the steps in name order.
    """
    activities: list[Callable[..., Awaitable[object]]] = [_inline_step_activity(registry)]
    for name in sorted(_published_names(registry)):
        impl = registry.get(name)
        if impl is not None:
            activities.append(_as_activity(impl))
    return activities


def _as_activity(impl: StepImplementation) -> Callable[..., Awaitable[object]]:
    @functools.wraps(impl.function)
    async def perform_step(*args: object) -> object:
        return await impl.function(*args)

    return activity.defn(name=impl.name)(perform_step)


def _inline_step_activity(registry: StepRegistry) -> Callable[[InlineStepCall], Awaitable[object]]:
    """The activity that builds a workflow's inline step on this worker's registry and performs it."""

    async def perform_inline_step(call: InlineStepCall) -> object:
        extended, problems = registry.including(call.spec)
        impl = extended.get(call.step)
        if impl is None:
            why = ". ".join(f"{where}: {detail}" for where, detail in problems) or "the spec does not declare it"
            msg = f"this worker cannot build step {call.step!r} from the spec it was handed: {why}"
            raise ApplicationError(msg, non_retryable=True)
        kwargs = {name: TypeAdapter(impl.input_types[name]).validate_python(value) for name, value in call.arguments.items()}
        if call.identity is None:
            return await impl.function(**kwargs)
        identity = _identity_from_wire(call.identity, extended)
        if step_conversation_of(identity) is None:
            with performing_step(identity):
                return await impl.function(**kwargs)
        continuing = _step_conversation_continuation(call, identity)
        try:
            with performing_step(identity), continuing_step_conversation(continuing):
                produced = await impl.function(**kwargs)
        except (
            ContinuationInFlightError,
            ContinuationConflictError,
            ContinuationUnsafeReplayError,
            ContinuationPayloadTooLargeError,
        ) as refusal:
            raise _refused_continuation(continuing.attempt, refusal) from refusal
        return InlineStepOutcome(output=json_output(produced, impl.output_type), continuation=continuing.committed)

    return activity.defn(name=PERFORM_INLINE_STEP)(perform_inline_step)


def _refused_continuation(attempt: ContinuationAttempt, refusal: Exception) -> ApplicationError:
    """A refused durable turn as a final Temporal failure, naming the attempt, its run and the reason."""
    msg = (
        f"attempt {attempt.attempt_id!r} of conversation {attempt.conversation_id} under run "
        f"{attempt.run_id!r} was refused: {refusal}"
    )
    return ApplicationError(msg, type=REFUSED_CONTINUATION, non_retryable=True)


_JSON: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
"""What a committed output is checked against before it is stored or returned."""


def json_output(produced: object, output_type: type) -> JsonValue:
    """One step's output as the JSON its step conversation stores and its workflow decodes back.

    It is dumped through the step's own annotation, so a completed duplicate returns the very
    payload a fresh run would have produced.

    Args:
        produced: What the step returned.
        output_type: The step's output annotation.

    Returns:
        The output as JSON.
    """
    return _JSON.validate_python(TypeAdapter[object](output_type).dump_python(produced, mode="json"))


def _delivery() -> tuple[str, str]:
    """Which workflow execution and scheduled activity this delivery belongs to, both empty outside an activity."""
    if not activity.in_activity():
        return ("", "")
    info = activity.info()
    if info.workflow_run_id is None:
        msg = f"activity {info.activity_id!r} carries no workflow execution, so the line it would continue cannot be addressed"
        raise ApplicationError(msg, non_retryable=True)
    return (info.workflow_run_id, info.activity_id)


def _step_conversation_continuation(call: InlineStepCall, identity: StepIdentity) -> StepConversationContinuation:
    """Where this delivery stands in its step conversation: the attempt it is, and the checkpoint it resumes."""
    execution, scheduled = _delivery()
    item_key = identity.item_key or ""
    turn_index = identity.turn.index if identity.turn is not None else 0
    conversation = continuation_conversation(execution, identity.node_id, item_key)
    attempt_id = continuation_attempt_id(
        workflow_execution=execution,
        scheduled_activity_id=scheduled,
        node_id=identity.node_id,
        item_key=item_key,
        turn_index=turn_index,
        logical_retry=len(identity.retry_feedback),
    )
    return StepConversationContinuation(
        attempt=ContinuationAttempt(
            conversation_id=conversation,
            attempt_id=attempt_id,
            expected_revision=call.continuation.revision if call.continuation is not None else 0,
            run_id=continuation_run_id(conversation, attempt_id),
        ),
        given=call.continuation,
    )


def sandbox_runner() -> SandboxedWorkflowRunner:
    """The workflow sandbox, with `core.ai.workflows` and `annotated_types` passed through.

    Workflow code reaches the worker's registry through the passthrough, and the sandbox neither
    isolates this package's module state between runs nor polices its imports. `core` and `core.ai`
    are not passed through and load inside the sandbox at worker startup, so `core.ai.__init__` and
    everything it imports must stay import-light. **The sandbox barely restricts a `replay_safe`
    step**: its `datetime.now`, `random`, `uuid4`, `os.environ` and file writes all run
    unrestricted, so `replay_safe` is an unchecked assertion. Temporal's deterministic event loop
    still applies, so `asyncio.to_thread` raises `NotImplementedError`.

    Returns:
        The sandboxed workflow runner.
    """
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules("core.ai.workflows", "annotated_types")
    )


async def connect(target_host: str, *, namespace: str = "default", logfire: bool = False) -> Client:
    """A Temporal client wired for Pydantic AI.

    `PydanticAIPlugin` installs `pydantic_data_converter`, so pydantic models and dataclasses
    cross activity boundaries as themselves, and configures the workflow sandbox for pydantic-ai.
    It is applied to the client only and a worker inherits it. Never pass it to a worker as well,
    or Temporal warns that it may run twice.

    Args:
        target_host: The Temporal server's host and port.
        namespace: The Temporal namespace to connect to.
        logfire: Whether to add `LogfirePlugin` for tracing.

    Returns:
        The connected client.
    """
    plugins: list[Plugin] = [PydanticAIPlugin()]
    if logfire:
        plugins.append(LogfirePlugin())
    return await Client.connect(target_host, namespace=namespace, plugins=plugins)


def worker(
    client: Client,
    registry: StepRegistry,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    activity_config: ActivityConfig | None = None,
    # `Never` for the deps parameter, because `AgentDepsT` is contravariant: `AbstractAgent[object, ...]`
    # is the NARROWEST agent type, not the widest, so an ordinary `Agent[None, Answer]` does not satisfy
    # it. `Never` is what "any agent, whatever it depends on" spells here. Output is covariant, so
    # `object` is already the widest there.
    agents: Sequence[AbstractAgent[Never, object]] = (),
) -> Worker:
    """A worker that serves durable runs of any workflow this registry validates.

    Args:
        client: A client from `connect`, so the pydantic payload converter is installed.
        registry: The steps this worker can execute. Published for its workflows by `bind`.
        task_queue: Which queue to serve. Runs must be started on the same one.
        activity_config: Timeouts and retry policy for every step activity.
        agents: Every agent a `replay_safe` step runs, so its model-request, tool-call and event
            activities are registered here. List them or the run silently loses the finer run log
            and repeats whole agent loops.

    Returns:
        The worker, with `registry` bound to `task_queue`.

    Raises:
        UserError: If an agent in `agents` carries no `TemporalDurability`, raised by
            `AgentPlugin` at construction. An agent a step runs but nobody listed is not
            detected here.
    """
    bind(task_queue, registry, activity_config)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[DurableWorkflowRun],
        activities=step_activities(registry),
        workflow_runner=sandbox_runner(),
        plugins=[AgentPlugin(agent) for agent in agents],
    )
