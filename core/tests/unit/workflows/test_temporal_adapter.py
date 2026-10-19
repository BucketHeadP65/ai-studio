"""The Temporal adapter's pure parts, provable without a server.

The durability claims live in `tests/integration/`; these pin the pieces that would silently
mis-wire a run long before a server saw it -- argument order across the activity boundary,
the step-to-activity table, the replay-safe step's route around that table, and the compile
determinism replay depends on.
"""

from __future__ import annotations

import inspect

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from pydantic import BaseModel, TypeAdapter, ValidationError
from temporalio import workflow
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.testing import ActivityEnvironment

from core.ai.history.contracts import ContinuationAttempt, ContinuationInFlightError, ContinuationPayloadTooLargeError, ContinuationRef
from core.ai.history.domain import CONTINUATION_REF_BYTES
from core.ai.workflows.contracts import (
    CURRENT_STEP,
    DEFAULT_TASK_QUEUE,
    Briefing,
    DurableWorkflowRun,
    FromNode,
    FromParameter,
    GateDecision,
    GateVerdict,
    ParameterSpec,
    PersonGate,
    RetryExhaustedError,
    RetryFeedback,
    ReviewedArtifacts,
    RunNode,
    RunRequest,
    Shape,
    StepConversationContinuation,
    StepIdentity,
    StepRegistry,
    TurnFacts,
    Workflow,
    WorkflowState,
    build_graph,
    observed_outcome,
    performing_step,
    spec_identity,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.temporal import (
    _DEFAULT_ACTIVITY_CONFIG,
    CONTINUING_STEP_CONVERSATION,
    EXHAUSTED_GATE,
    PERFORM_INLINE_STEP,
    REFUSED_CONTINUATION,
    WORKFLOW_NAMESPACE,
    WORKFLOW_TYPE,
    ActivityExecutor,
    IdentityOnTheWire,
    InlineStepCall,
    InlineStepOutcome,
    ScheduledWork,
    TurnOnTheWire,
    _binding,
    _exhausted_gate,
    _identity_from_wire,
    _identity_on_wire,
    _inline_step_activity,
    _positional,
    _published_names,
    _refused_continuation,
    bind,
    continuation_attempt_id,
    continuation_conversation,
    continuation_run_id,
    step_activities,
    step_conversation_of,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from temporalio.activity import Info as ActivityInfo
from tests.unit.workflows.conftest import ApproveAll, CallLog, SalesData, register_quarterly_steps


@pytest.fixture
def registry(call_log: CallLog) -> StepRegistry:
    reg = StepRegistry()
    register_quarterly_steps(reg, call_log)
    return reg


SUMMARISE = Briefing(
    name="summarise_sales",
    instructions="Read the quarter's sales and write one sentence about them a board member could read aloud.",
    inputs={"sales": SameAs(like="fetch_sales")},
    output=Shape(json_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
)

BROUGHT = Workflow(
    name="quarterly-summary",
    version=1,
    description="Fetch sales, then summarise them with a step this artifact brought with it.",
    parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
    inline=(SUMMARISE,),
    nodes=(
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="summarise_sales", work="summarise_sales", inputs={"sales": FromNode(node="fetch_sales")}),
    ),
    output=FromNode(node="summarise_sales"),
)


def _executor(published: frozenset[str] = frozenset(), spec: Workflow = BROUGHT) -> ActivityExecutor:
    return ActivityExecutor(workflow=spec, published=published, activity_config=_DEFAULT_ACTIVITY_CONFIG)


def _activity_names(registry: StepRegistry) -> list[str]:
    return sorted(getattr(a, "__temporal_activity_definition").name for a in step_activities(registry))


def _briefing_host(registry: StepRegistry, seen: list[object]) -> StepRegistry:
    """The same steps, plus a `perform` that records its inputs and returns a fixed value.

    A fixed value rather than a model call: what these tests are about is the wiring of a
    brought step, not what a model would say.
    """

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        seen.append((task.name, inputs.get("sales")))
        return TypeAdapter(output_type).validate_python({"text": "read"})

    host = registry.copy()
    host.perform = perform
    return host


async def test_a_replay_safe_step_is_performed_in_workflow_code(call_log: CallLog) -> None:
    """The granularity change, at its smallest: a marked step is awaited, not scheduled.

    Running here is what puts the step's agent in workflow code, where `TemporalDurability`
    turns each of ITS model requests into an activity. This assertion is made outside any
    workflow precisely because a step that reached `workflow.execute_activity` could not
    have answered at all -- see the sibling test.
    """
    registry = StepRegistry()

    async def think(prompt: str) -> str:
        call_log.calls.append("think")
        return prompt.upper()

    impl = registry.register(think, replay_safe=True)
    assert await _executor().perform("think", impl, {"prompt": "hello"}) == "HELLO"
    assert call_log.calls == ["think"], "the step's own body ran, in this frame"


async def test_an_unmarked_step_is_still_scheduled_as_an_activity(registry: StepRegistry, call_log: CallLog) -> None:
    """The default route is untouched: an unmarked step reaches `workflow.execute_activity`.

    Outside a workflow that call refuses, which is the assertion -- the executor got as far as
    asking Temporal to schedule work rather than quietly running the body here.
    """
    impl = registry.get("draft_report")
    assert impl is not None
    with pytest.raises(TemporalError, match="Not in workflow event loop"):
        await _executor(published=frozenset({"draft_report"})).perform("draft_report", impl, {"ledger": None})
    assert call_log.calls == [], "an unmarked step's body must not run in workflow code"


def test_a_replay_safe_step_gets_no_activity_of_its_own() -> None:
    """A step that runs in workflow code has no activity, so the worker must not advertise one.

    Registering it anyway would leave a wrapper that runs the whole agent inside a single
    activity -- the exact coarse-grained behaviour the marker exists to escape -- reachable by
    anything that scheduled it by name.
    """
    registry = StepRegistry()

    async def think(prompt: str) -> str:
        return prompt

    async def publish(text: str) -> str:
        return text

    registry.register(think, replay_safe=True)
    registry.register(publish)
    assert _activity_names(registry) == sorted([PERFORM_INLINE_STEP, "publish"])


def test_arguments_cross_in_the_steps_declared_order(registry: StepRegistry) -> None:
    """Temporal carries activity arguments positionally, so kwargs must be ordered by signature.

    `reconcile(sales, costs)` wired from a spec whose inputs happen to be written costs-first
    would otherwise arrive swapped, and both are dataclasses the converter would happily decode
    into the wrong parameter.
    """
    impl = registry.get("reconcile")
    assert impl is not None
    args = _positional(impl, {"costs": "C", "sales": "S"})
    assert args == ["S", "C"], "declared order wins over the order the spec happened to wire"


def test_an_unwired_optional_parameter_takes_its_default() -> None:
    """A step may declare a parameter the spec does not wire; the activity still needs a value for it."""
    registry = StepRegistry()

    async def summarise(text: str, limit: int = 10) -> str:
        return text[:limit]

    registry.register(summarise)
    impl = registry.get("summarise")
    assert impl is not None
    assert _positional(impl, {"text": "hello"}) == ["hello", 10]


def test_every_step_becomes_an_activity_named_after_it(registry: StepRegistry) -> None:
    assert _activity_names(registry) == sorted([*registry.names, PERFORM_INLINE_STEP])


def test_a_published_step_is_scheduled_under_its_own_name(registry: StepRegistry) -> None:
    """The route that works is untouched: name and positional arguments, exactly as before."""
    impl = registry.get("reconcile")
    assert impl is not None
    scheduled = _executor(published=_published_names(registry)).scheduling(impl, {"costs": "C", "sales": "S"})
    assert scheduled.activity == "reconcile"
    assert scheduled.args == ["S", "C"]


def test_a_step_the_worker_never_published_is_scheduled_through_the_generic_activity(registry: StepRegistry) -> None:
    """The defect, at its smallest: a brought step has no activity of its own to be scheduled under.

    The worker's table is fixed from the base registry, and `summarise_sales` reaches the
    executor only because the spec extended that registry inside the workflow. Scheduling it by
    name would name an activity nobody registered, so it is scheduled through the one activity
    that can rebuild it -- carrying the spec, since the worker alone cannot.
    """
    extended, problems = _briefing_host(registry, []).including(BROUGHT)
    assert problems == ()
    impl = extended.get("summarise_sales")
    assert impl is not None
    assert "summarise_sales" not in _published_names(registry), "the worker's table cannot contain it"

    scheduled = _executor(published=_published_names(registry)).scheduling(impl, {"sales": SalesData(quarter="Q2", total=1000)})
    assert scheduled.activity == PERFORM_INLINE_STEP
    (call,) = scheduled.args
    assert isinstance(call, InlineStepCall)
    assert call.step == "summarise_sales"
    assert call.spec == BROUGHT, "the activity cannot rebuild the step without the spec that declared it"
    assert call.arguments == {"sales": SalesData(quarter="Q2", total=1000)}


def test_scheduling_a_brought_step_saves_whatever_identity_is_published(registry: StepRegistry) -> None:
    """`scheduling` reads `CURRENT_STEP` and carries it on the call, `TurnFacts.outputs` dropped -- a type is not data."""
    extended, problems = _briefing_host(registry, []).including(BROUGHT)
    assert problems == ()
    impl = extended.get("summarise_sales")
    assert impl is not None

    facts = TurnFacts(work="summarise_sales", index=1, turns=("digest", "summarise_sales"), each="item", tools=("search",), outputs=(str,))
    identity = StepIdentity(node_id="each", item_key="3", turn=facts, tier="helper")
    with performing_step(identity):
        scheduled = _executor(published=_published_names(registry)).scheduling(impl, {"sales": SalesData(quarter="Q2", total=1000)})

    (call,) = scheduled.args
    assert isinstance(call, InlineStepCall)
    assert call.identity == IdentityOnTheWire(
        node_id="each",
        item_key="3",
        turn=TurnOnTheWire(work="summarise_sales", index=1, turns=("digest", "summarise_sales"), each="item", tools=("search",)),
        tier="helper",
    )


def test_scheduling_a_brought_step_leaves_the_identity_unset_with_nothing_published(registry: StepRegistry) -> None:
    extended, problems = _briefing_host(registry, []).including(BROUGHT)
    assert problems == ()
    impl = extended.get("summarise_sales")
    assert impl is not None
    assert CURRENT_STEP.get() is None

    scheduled = _executor(published=_published_names(registry)).scheduling(impl, {"sales": SalesData(quarter="Q2", total=1000)})
    (call,) = scheduled.args
    assert isinstance(call, InlineStepCall)
    assert call.identity is None


def test_what_the_worker_publishes_is_what_its_workflows_believe_it_publishes(registry: StepRegistry) -> None:
    """The two halves of the fix read one rule, so the routing cannot decide against the table.

    A `published` set that disagreed with the activity table would either schedule an
    unregistered name or send a perfectly good step the long way round.
    """
    bind("agreement", registry)
    published = _binding("agreement").published
    assert published == frozenset(name for name in _activity_names(registry) if name != PERFORM_INLINE_STEP)


async def test_the_generic_activity_rebuilds_the_step_and_hands_it_its_declared_types(registry: StepRegistry) -> None:
    """What the activity is for: the spec's declaration plus the host's terms make the real step.

    Arguments arrive as the JSON they were serialised to, so the assertion that matters is that
    `sales` reaches the step as a `SalesData` -- the type its `like` reference named -- rather
    than as the dict on the wire.
    """
    seen: list[object] = []
    run = _inline_step_activity(_briefing_host(registry, seen))
    result = await run(InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}))

    assert seen == [("summarise_sales", SalesData(quarter="Q2", total=1000))]
    assert isinstance(result, BaseModel), "the step's output is the type its own `Shape` declared"
    assert result.model_dump() == {"text": "read"}


async def test_the_generic_activity_refuses_a_step_the_spec_never_declared(registry: StepRegistry) -> None:
    """A worker handed a spec it cannot build the step from must fail the run, not retry forever.

    Temporal's default retry policy is unbounded, so a retryable failure here would leave the
    run alive and going nowhere. The refusal names the step and carries the build's own reasons.
    """
    run = _inline_step_activity(registry)
    with pytest.raises(ApplicationError) as exc_info:
        await run(InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={}))

    assert exc_info.value.non_retryable is True
    assert "summarise_sales" in str(exc_info.value)
    assert "cannot perform a briefing" in str(exc_info.value), "the reason the build refused reaches the history"


async def test_the_generic_activity_republishes_the_identity_the_workflow_scheduled_it_under(registry: StepRegistry) -> None:
    """The step runs under the SAME identity it would in-process, `outputs` rebuilt from this worker's own registry."""
    seen: list[StepIdentity | None] = []

    async def recording_perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        published = CURRENT_STEP.get()
        assert published is not None
        assert published.turn is not None
        assert published.turn.outputs == (output_type,), "outputs are rebuilt from this worker's own extended registry"
        seen.append(published)
        return TypeAdapter(output_type).validate_python({"text": "read"})

    host = registry.copy()
    host.perform = recording_perform
    run = _inline_step_activity(host)

    identity = IdentityOnTheWire(
        node_id="each",
        item_key="3",
        turn=TurnOnTheWire(work="summarise_sales", index=0, turns=("summarise_sales",), each="item", tools=()),
        tier="helper",
    )
    assert CURRENT_STEP.get() is None
    result = await run(
        InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=identity)
    )

    assert CURRENT_STEP.get() is None, "unpublished again once the activity returns"
    assert isinstance(result, InlineStepOutcome), "a call that is one turn of a line answers with its outcome"
    assert result.output == {"text": "read"}, "the step's own output, as the JSON the workflow decodes back into its type"
    (republished,) = seen
    assert republished is not None
    assert republished.node_id == "each"
    assert republished.item_key == "3"
    assert republished.tier == "helper"
    assert republished.turn is not None
    assert republished.turn.work == "summarise_sales"
    assert republished.turn.turns == ("summarise_sales",)


async def test_the_generic_activity_leaves_current_step_unset_with_no_identity_on_the_call(registry: StepRegistry) -> None:
    """A call the workflow scheduled with nothing published performs the step the same way, unpublished."""
    seen: list[StepIdentity | None] = []

    async def recording_perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        seen.append(CURRENT_STEP.get())
        return TypeAdapter(output_type).validate_python({"text": "read"})

    host = registry.copy()
    host.perform = recording_perform
    run = _inline_step_activity(host)

    await run(InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}))

    assert seen == [None]


async def test_the_generic_activity_refuses_a_turn_name_the_republished_identity_cannot_resolve(registry: StepRegistry) -> None:
    """A turn named in the identity must resolve on this worker's extended registry, or the run must not go on not knowing why."""
    run = _inline_step_activity(_briefing_host(registry, []))

    identity = IdentityOnTheWire(
        node_id="each",
        item_key="0",
        turn=TurnOnTheWire(work="summarise_sales", index=1, turns=("summarise_sales", "no_such_turn"), each="item", tools=()),
    )
    with pytest.raises(ApplicationError) as exc_info:
        await run(
            InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=identity)
        )

    assert exc_info.value.non_retryable is True
    assert "no_such_turn" in str(exc_info.value)


def test_registering_activities_leaves_the_registrys_callables_alone(registry: StepRegistry) -> None:
    """A registry handed to a worker must still be runnable in-process afterwards.

    The step is wrapped rather than decorated in place, so the callable the in-process engine
    awaits never acquires Temporal's activity marker.
    """
    impl = registry.get("publish")
    assert impl is not None
    step_activities(registry)
    assert not hasattr(impl.function, "__temporal_activity_definition")


async def test_the_activity_wrapper_keeps_the_steps_signature(registry: StepRegistry) -> None:
    """The payload converter types arguments off the activity's signature, so it must survive wrapping."""
    impl = registry.get("draft_report")
    assert impl is not None
    (wrapper,) = [a for a in step_activities(registry) if getattr(a, "__temporal_activity_definition").name == "draft_report"]
    assert list(inspect.signature(wrapper).parameters) == list(inspect.signature(impl.function).parameters)
    assert inspect.get_annotations(wrapper, eval_str=True) == inspect.get_annotations(impl.function, eval_str=True)


def test_an_unbound_task_queue_says_so(registry: StepRegistry) -> None:
    """A worker built by hand without `bind` would otherwise fail deep inside compilation."""
    with pytest.raises(RuntimeError) as exc_info:
        _binding("nobody-serves-this")
    assert "nobody-serves-this" in str(exc_info.value)

    bind("somebody-serves-this", registry)
    assert _binding("somebody-serves-this").registry is registry


def test_a_step_that_can_never_succeed_stops_retrying_by_default(registry: StepRegistry) -> None:
    """Temporal retries without end unless told otherwise, so a permanent failure would read as a run still working.

    The default caps the attempts and names the failures no retry can fix: an activity the worker
    never registered, and an argument that does not fit the step.
    """
    bind("default-config", registry)
    policy = _binding("default-config").activity_config.get("retry_policy")

    assert policy is not None, "no retry policy means Temporal's unbounded default"
    assert 1 <= policy.maximum_attempts <= 10, "maximum_attempts=0 is Temporal's spelling of no ceiling"
    assert {"NotFoundError", "TypeError", "ValidationError"} <= set(policy.non_retryable_error_types or ())


def test_the_workflow_type_is_a_stable_name() -> None:
    """A running workflow outlives a deploy, so its registered type name is a compatibility surface."""
    assert WORKFLOW_TYPE == "PydanticAIWorkflowRun"
    assert DEFAULT_TASK_QUEUE == "pydantic-ai-workflows"


def test_no_placeholder_node_id_reaches_the_built_graph(registry: StepRegistry) -> None:
    """Compilation mints random placeholder ids internally; none may survive into the graph.

    `pydantic_graph` labels a broadcast fork and every `match` branch with
    `__placeholder__:<label>:<uuid4>` when the caller does not name them, and neither has an
    API to name them. Those ids are remapped deterministically before the graph is built, which
    is the only reason replaying a compile inside a Temporal workflow reproduces the same graph.
    One leaking through would make a replayed run diverge from its own history.
    """
    spec = Workflow(
        name="two-roots-and-a-gate",
        version=1,
        description="Two independent roots force a broadcast fork; the gate forces a match decision.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="draft_report", work="draft_report", inputs={"ledger": FromNode(node="reconcile")}),
            PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
            RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")}),
        ),
        output=FromNode(node="publish"),
    )
    first = build_graph(spec, registry, approval=ApproveAll()).render()
    second = build_graph(spec, registry, approval=ApproveAll()).render()
    assert "__placeholder__" not in first
    assert first == second


async def test_an_auto_approved_gate_answers_itself_and_is_recorded_apart() -> None:
    """Automode passes the gate without a signal, and the evidence says a machine did it.

    Never a `gates_approved` entry: trust derivation counts those, and a gate nobody was asked
    about must not read as a person's approval.
    """
    run = DurableWorkflowRun()
    assert run.gate_mode == "wait", "asking a person is the default"
    run.gate_mode = "auto"

    assert (await run.review("approve", "a draft")).source == "auto"
    assert run.waiting_at() is None, "an auto-approved gate never waits, so no panel shows a question nobody was asked"

    evidence = run.evidence()
    assert evidence.gates_auto_approved == ["approve"]
    assert evidence.gates_approved == []


async def test_a_gate_still_waits_for_a_person_when_automode_is_off() -> None:
    """The default route is untouched: the gate waits and waits on a signal.

    Outside a workflow that wait refuses, which is the assertion -- the gate got as far as asking
    Temporal to wait rather than quietly answering itself.
    """
    run = DurableWorkflowRun()

    with pytest.raises(TemporalError, match="Not in workflow event loop"):
        await run.review("approve", "a draft")

    waiting = run.waiting_at()
    assert waiting is not None
    assert waiting.gate_id == "approve"


def test_a_run_request_asks_a_person_unless_it_says_otherwise() -> None:
    """The gate mode travels with the request, so what a run does about gates is in its own history."""
    spec = Workflow(
        name="one-step",
        version=1,
        description="A single step, enough to build a request around.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="fetch_sales"),
    )
    assert RunRequest(spec=spec, params={}, workflow_id="wf.one").gate_mode == "wait"
    assert RunRequest(spec=spec, params={}, workflow_id="wf.one", gate_mode="auto").gate_mode == "auto"
    with pytest.raises(ValidationError, match="gate_mode"):
        RunRequest.model_validate({"spec": spec.model_dump(mode="json"), "params": {}, "workflow_id": "wf.one", "gate_mode": "orchestrate"})


def test_a_run_requests_arguments_cross_the_wire_as_the_json_they_are() -> None:
    """An array stays an array and a boolean stays a boolean once the request is serialised and read back."""
    spec = Workflow(
        name="typed-inputs",
        version=1,
        description="One step over an array, a boolean and a number.",
        parameters={
            "knowledge_bases": ParameterSpec(
                description="which knowledge bases to read", json_schema={"type": "array", "items": {"type": "string"}}
            ),
            "include_rejected": ParameterSpec(description="keep what was ruled out", json_schema={"type": "boolean"}),
            "options_wanted": ParameterSpec(description="how many options", json_schema={"type": "integer"}),
        },
        nodes=(RunNode(id="fetch_sales", work="fetch_sales", inputs={}),),
        output=FromNode(node="fetch_sales"),
    )
    params = {"knowledge_bases": ["research", "repo-docs"], "include_rejected": True, "options_wanted": 3}

    crossed = RunRequest.model_validate_json(RunRequest(spec=spec, params=params, workflow_id="wf.typed").model_dump_json())

    assert crossed.params == params
    assert crossed.params["knowledge_bases"] == ["research", "repo-docs"]
    assert crossed.params["include_rejected"] is True
    assert crossed.params["options_wanted"] == 3


def test_a_run_request_recorded_with_text_arguments_still_decodes() -> None:
    """The shape every durable history before typed arguments holds: strings, unchanged."""
    spec = Workflow(
        name="one-step",
        version=1,
        description="A single step, enough to build a request around.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="fetch_sales"),
    )
    recorded = RunRequest(spec=spec, params={"quarter": "Q1"}, workflow_id="wf.one").model_dump_json()

    assert RunRequest.model_validate_json(recorded).params == {"quarter": "Q1"}


# --- a repair crosses the durable boundary whole -------------------------------------------


def _refused(run_id: str = "durable-run", *, digest: str | None = None) -> tuple[RetryFeedback, ReviewedArtifacts]:
    """One rejection over `"draft A"`, and the run's own store holding that value."""
    artifacts = ReviewedArtifacts(run_id=run_id)
    ref = artifacts.persist("draft", None, "draft A")
    if digest is not None:
        ref = ref.model_copy(update={"value_digest": digest})
    feedback = RetryFeedback(
        gate_id="review", source="person", target_node="draft", attempt=1,
        feedback="missing evidence", reviewed=ref, requested_repair="cite the constraint",
    )
    return feedback, artifacts


def test_a_repair_crosses_the_activity_boundary_with_the_value_it_was_refused_over() -> None:
    """The activity runs in another process, so the value travels with the call, not as a reference."""
    feedback, artifacts = _refused()
    identity = StepIdentity(
        node_id="draft", retry_feedback=(feedback,), parameters_json='{"topic":"original"}', reviewed_artifacts=artifacts
    )

    wire = _identity_on_wire(identity)

    assert wire is not None
    restored = _identity_from_wire(IdentityOnTheWire.model_validate_json(wire.model_dump_json()), StepRegistry())
    assert restored.retry_feedback == (feedback,)
    assert restored.parameters_json == identity.parameters_json
    assert restored.reviewed_artifacts is not None
    assert restored.reviewed_artifacts.read(feedback.reviewed) == "draft A"


def test_a_reviewed_value_that_does_not_match_its_digest_is_refused_for_good() -> None:
    """Bytes and digest disagreeing means the repair would answer over something nobody reviewed."""
    feedback, _artifacts = _refused(digest="f" * 64)
    wire = IdentityOnTheWire(node_id="draft", retry_feedback=(feedback,), reviewed_values=('"draft A"',))

    with pytest.raises(ApplicationError, match="digest") as raised:
        _identity_from_wire(wire, StepRegistry())

    assert raised.value.non_retryable, "the same call would carry the same contradiction again"


def test_a_step_owing_a_repair_takes_the_route_that_can_carry_it(registry: StepRegistry) -> None:
    """A published step's own activity is typed on its arguments and has nowhere to put a rejection."""
    impl = registry.get("draft_report")
    assert impl is not None
    feedback, artifacts = _refused()
    published = _published_names(registry)
    assert "draft_report" in published

    with performing_step(StepIdentity(node_id="draft_report", retry_feedback=(feedback,), reviewed_artifacts=artifacts)):
        scheduled = _executor(published=published).scheduling(impl, {"ledger": None})

    assert scheduled.activity == PERFORM_INLINE_STEP
    (call,) = scheduled.args
    assert isinstance(call, InlineStepCall)
    assert call.identity is not None
    assert call.identity.retry_feedback == (feedback,)
    assert call.identity.reviewed_values == ('"draft A"',)


def test_a_spent_gate_ceiling_fails_the_durable_run_with_the_whole_repair_on_it() -> None:
    """The one place an unresolved repair can go on a durable run: the failure a client reads."""
    feedback, _artifacts = _refused()

    failure = _exhausted_gate(RetryExhaustedError("review", "draft", 3, feedback))

    assert failure.type == EXHAUSTED_GATE
    assert failure.non_retryable, "every reach the gate had is spent; running it again changes nothing"
    assert "review" in str(failure)
    (detail,) = failure.details
    assert detail == feedback.model_dump(mode="json"), "the ending holds the rejection whole, reviewed value included"


async def test_a_gate_refused_by_signal_spends_its_reaches_and_keeps_the_signals_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable loop end to end: a signalled rejection reruns the draft until the ceiling is spent.

    `wait_condition` is Temporal's own park, replaced here by an assertion that the verdict this
    gate waits for is already in: outside a workflow there is no event loop to park on.
    """
    run = DurableWorkflowRun()
    drafted: list[str] = []

    async def wait_condition(condition: Callable[[], bool]) -> None:
        assert condition(), "the gate parked on a verdict that never arrived"

    monkeypatch.setattr(workflow, "wait_condition", wait_condition)

    async def draft() -> str:
        drafted.append("draft A")
        return "draft A"

    class Signalled:
        """The browser's own decision, arriving as the signal a worker restart would survive."""

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            run.decide(GateVerdict(gate_id=gate_id, approved=False, feedback="missing evidence", requested_repair="cite the constraint"))
            return await run.review(gate_id, value, note=note)

    registry = StepRegistry()
    registry.register(draft)
    spec = Workflow(
        name="durable-exhaustion",
        version=1,
        description="A gate refused by signal at every reach it has.",
        nodes=(RunNode(id="draft", work="draft"), PersonGate(id="review", reviews=FromNode(node="draft"), rejected_to="draft")),
        output=FromNode(node="review"),
    )
    state = WorkflowState(params={})

    with pytest.raises(RetryExhaustedError) as raised:
        await build_graph(spec, registry, approval=Signalled()).run(state=state)

    assert raised.value.attempts == 3
    assert raised.value.feedback.requested_repair == "cite the constraint"
    assert len(drafted) == 3
    assert [(fact.source, fact.verdict) for fact in state.gate_decisions] == [("person", "reject")] * 3
    assert run.evidence().gates_rejected == {"review": 3}


def _a_turn(*, index: int = 0, item_key: str = "3") -> IdentityOnTheWire:
    """One map item's turn, as the workflow publishes it onto a brought-step call."""
    return IdentityOnTheWire(
        node_id="digest_each",
        item_key=item_key,
        turn=TurnOnTheWire(work="summarise_sales", index=index, turns=("summarise_sales",), each="item", tools=()),
    )


def _an_info(*, attempt: int = 1, activity_id: str = "5", workflow_run_id: str = "execution-a") -> ActivityInfo:
    """Temporal's own facts about one delivery, with the three this task's identities read varied."""
    return replace(
        ActivityEnvironment.default_info(), attempt=attempt, activity_id=activity_id, workflow_run_id=workflow_run_id
    )


def _continuing_host(registry: StepRegistry, seen: list[StepConversationContinuation | None]) -> StepRegistry:
    """A host whose `perform` records the line it was published under and settles it with a ref of its own."""

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        continuing = CONTINUING_STEP_CONVERSATION.get()
        seen.append(continuing)
        if continuing is not None:
            continuing.committed = ContinuationRef(
                conversation_id=continuing.attempt.conversation_id,
                revision=continuing.attempt.expected_revision + 1,
                run_id=continuing.attempt.run_id,
                snapshot_seq=2,
                checkpoint_digest="d" * 64,
            )
        return TypeAdapter(output_type).validate_python({"text": "read"})

    host = registry.copy()
    host.perform = perform
    return host


def test_an_inline_step_call_carries_an_address_and_never_a_transcript() -> None:
    """The whole bound on the payload: a ref names where the messages are, and nothing carries them."""
    ref = ContinuationRef(
        conversation_id=continuation_conversation("execution-a", "digest_each", "3"),
        revision=2,
        run_id="run-a",
        snapshot_seq=7,
        checkpoint_digest="e" * 64,
    )
    call = InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={}, identity=_a_turn(), continuation=ref)

    assert InlineStepCall.model_fields["continuation"].annotation == (ContinuationRef | None)
    assert set(ContinuationRef.model_fields) == {"conversation_id", "revision", "run_id", "snapshot_seq", "checkpoint_digest"}
    on_the_wire = call.model_dump_json()
    assert "messages" not in on_the_wire
    assert '"parts"' not in on_the_wire
    assert len(ref.model_dump_json().encode()) <= CONTINUATION_REF_BYTES


def test_a_sequences_skills_cross_the_activity_boundary_and_come_back_whole(registry: StepRegistry) -> None:
    """The union a workflow computed is what the activity mounts, so the two processes ask one surface."""
    extended, problems = _briefing_host(registry, []).including(BROUGHT)
    assert problems == ()
    facts = TurnFacts(
        work="summarise_sales",
        index=1,
        turns=("summarise_sales",),
        each="item",
        tools=("search",),
        outputs=(str,),
        skills=("saving-a-workflow", "orchestrating-a-run"),
    )

    wire = _identity_on_wire(StepIdentity(node_id="digest_each", item_key="3", turn=facts, tier="helper"))
    assert wire is not None
    restored = _identity_from_wire(IdentityOnTheWire.model_validate_json(wire.model_dump_json()), extended)

    assert restored.turn is not None
    assert restored.turn.skills == ("saving-a-workflow", "orchestrating-a-run"), "the union survives the round trip in order"
    assert restored.turn.tools == ("search",)


def _addressed_as(identity: StepIdentity) -> tuple[tuple[str, str] | None, UUID, str]:
    """Where one published turn stands: its line, its conversation, and the attempt id of its first try."""
    assert identity.turn is not None
    return (
        step_conversation_of(identity),
        continuation_conversation("execution-a", identity.node_id, identity.item_key or ""),
        continuation_attempt_id(
            workflow_execution="execution-a",
            scheduled_activity_id="5",
            node_id=identity.node_id,
            item_key=identity.item_key or "",
            turn_index=identity.turn.index,
            logical_retry=0,
        ),
    )


def test_a_sequences_skills_never_reach_the_identity_a_line_is_addressed_by() -> None:
    """A conversation and an attempt are addressed by where a turn stands, never by what its worker holds.

    Otherwise mounting one more skill would give a line in flight a new identity and orphan every
    checkpoint it had already committed.
    """
    without = TurnFacts(work="summarise_sales", index=1, turns=("summarise_sales",), each="item", tools=(), outputs=(str,))
    holding = replace(without, skills=("saving-a-workflow",))

    addressed = _addressed_as(StepIdentity(node_id="digest_each", item_key="3", turn=without))

    assert addressed == _addressed_as(StepIdentity(node_id="digest_each", item_key="3", turn=holding))
    assert addressed == (
        ("digest_each", "3"),
        UUID("f5776b0b-8be7-55f5-88d0-d19a151aa3c2"),
        "c2dd61ce367a2fd63ae299301833da2662b1da8d15628c18e493d2d7fd0595b8",
    ), "the identities a stored checkpoint is found under, unchanged"


def test_a_call_that_continues_no_line_is_answered_with_the_steps_own_output() -> None:
    """A one-shot step continues no step conversation, so its answer is never an `InlineStepOutcome`."""
    assert _executor().continues(ScheduledWork(activity="draft_report", args=[])) is False


# --- literal fixtures -----------------------------------------------------------------------------
#
# Every string below is typed out by hand, never produced by dumping today's models, so a
# serializer change that starts writing a field by default fails loudly here.

_UNDECLARED_WORKFLOW_JSON = (
    '{"name":"quarterly-close","version":1,'
    '"description":"Collect the quarter\'s sales and have a person approve the total before the quarter counts as closed.",'
    '"nodes":[{"kind":"run","id":"collect_sales","work":"collect_sales"},'
    '{"kind":"gate","id":"approve_report","reviews":{"node":"collect_sales"},"rejected_to":"collect_sales"}],'
    '"output":{"node":"approve_report"}}'
)
"""A workflow that declares no `result` and leaves its gate's `max_attempts` unset: neither key appears
anywhere in this text."""

_UNDECLARED_WORKFLOW_HASH = "9ee0acd066126b4153869c77763a8604afc29ae7902f171d9510cc3572701908"
"""`spec_identity` of `_UNDECLARED_WORKFLOW_JSON`, computed once and pinned so a hashing change fails
loudly here rather than silently re-keying every evidence record filed under this identity."""


def test_a_workflow_that_declares_no_result_parses_and_keeps_its_identity() -> None:
    spec = Workflow.model_validate_json(_UNDECLARED_WORKFLOW_JSON)
    gate = next(node for node in spec.nodes if node.kind == "gate")

    assert spec.result is None, "a workflow that never declared its work cannot say what its result is"
    assert gate.max_attempts == 3, "the ceiling a gate takes when the workflow names none"
    assert spec_identity(spec) == _UNDECLARED_WORKFLOW_HASH, "the pinned literal, because a moved hash orphans this workflow's evidence"

    outcome = observed_outcome(spec.result, status="completed", facts=(), primary=None, decisions=(), reviews={})
    assert outcome.status == "unknown", "no declared work is honestly unknown, never inferred success"


def test_a_refused_continuation_fails_the_run_rather_than_retrying_forever() -> None:
    """Every redelivery of one scheduled activity carries the same attempt, so a retry would spin.

    The failure names the attempt, its conversation and its run, and carries its own type so a
    reader can tell it from any other run failure.
    """
    attempt = ContinuationAttempt(
        conversation_id=continuation_conversation("execution-a", "digest_each", "3"),
        attempt_id="a" * 64,
        expected_revision=1,
        run_id="run-a",
    )
    refusal = ContinuationInFlightError("attempt 'bbbb' is still running under run 'run-b'")

    failure = _refused_continuation(attempt, refusal)

    assert failure.non_retryable is True
    assert failure.type == REFUSED_CONTINUATION
    assert "a" * 64 in str(failure)
    assert "run-a" in str(failure)
    assert str(attempt.conversation_id) in str(failure)
    assert "is still running under run 'run-b'" in str(failure), "the reason the ledger gave reaches the history"


def test_only_a_turn_is_a_line() -> None:
    """A one-shot step continues nothing, which is what keeps every other call's wire shape unchanged."""
    assert step_conversation_of(None) is None
    assert step_conversation_of(StepIdentity(node_id="draft_report")) is None
    assert step_conversation_of(StepIdentity(node_id="digest_each", item_key="3", turn=TurnFacts(
        work="summarise_sales", index=1, turns=("summarise_sales",), each="item", tools=(), outputs=(str,)
    ))) == ("digest_each", "3")


def test_the_workflow_schedules_a_turn_against_the_ref_its_previous_turn_committed(registry: StepRegistry) -> None:
    """The one thing that crosses between two turns of one line: the address the earlier turn left."""
    executor = _executor(published=_published_names(registry))
    first = ContinuationRef(
        conversation_id=continuation_conversation("execution-a", "digest_each", "3"),
        revision=1,
        run_id="run-a",
        snapshot_seq=0,
        checkpoint_digest="f" * 64,
    )
    executor.conversations[("digest_each", "3")] = first
    brought = _briefing_host(registry, []).including(BROUGHT)[0].get("summarise_sales")
    assert brought is not None

    with performing_step(
        StepIdentity(
            node_id="digest_each",
            item_key="3",
            turn=TurnFacts(work="summarise_sales", index=1, turns=("summarise_sales",), each="item", tools=(), outputs=(str,)),
        )
    ):
        scheduled = executor.scheduling(brought, {"sales": SalesData(quarter="Q2", total=1000)})

    assert scheduled.activity == PERFORM_INLINE_STEP
    assert scheduled.conversation == ("digest_each", "3")
    (call,) = scheduled.args
    assert isinstance(call, InlineStepCall)
    assert call.continuation == first

    with performing_step(StepIdentity(node_id="digest_each", item_key="9", turn=TurnFacts(
        work="summarise_sales", index=0, turns=("summarise_sales",), each="item", tools=(), outputs=(str,)
    ))):
        other = executor.scheduling(brought, {"sales": SalesData(quarter="Q2", total=1000)})

    (call_of_another_item,) = other.args
    assert isinstance(call_of_another_item, InlineStepCall)
    assert call_of_another_item.continuation is None, "one item's line says nothing about another's"


async def test_two_deliveries_of_one_scheduled_activity_address_one_attempt(registry: StepRegistry) -> None:
    """Temporal retrying delivery must not manufacture a second logical attempt, or a turn would run twice."""
    seen: list[StepConversationContinuation | None] = []
    run = _inline_step_activity(_continuing_host(registry, seen))
    call = InlineStepCall(
        spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=_a_turn()
    )

    first = ActivityEnvironment()
    first.info = _an_info(attempt=1)
    delivered_once = await first.run(run, call)
    second = ActivityEnvironment()
    second.info = _an_info(attempt=2)
    delivered_again = await second.run(run, call)

    once, again = seen
    assert once is not None
    assert again is not None
    assert once.attempt.attempt_id == again.attempt.attempt_id, "the delivery attempt is not part of the identity"
    assert isinstance(delivered_once, InlineStepOutcome)
    assert isinstance(delivered_again, InlineStepOutcome)
    assert delivered_once.continuation == delivered_again.continuation
    assert once.attempt.conversation_id == continuation_conversation("execution-a", "digest_each", "3")
    assert once.attempt.run_id == continuation_run_id(once.attempt.conversation_id, once.attempt.attempt_id)


async def test_a_rescheduled_turn_is_a_new_attempt_on_the_same_line(registry: StepRegistry) -> None:
    """What the workflow itself schedules again is a new attempt: a rewind must not read an older turn's answer back."""
    seen: list[StepConversationContinuation | None] = []
    run = _inline_step_activity(_continuing_host(registry, seen))
    first = InlineStepCall(
        spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=_a_turn()
    )
    repaired = InlineStepCall(
        spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=_a_turn()
    )

    original = ActivityEnvironment()
    original.info = _an_info(activity_id="5")
    await original.run(run, first)
    rescheduled = ActivityEnvironment()
    rescheduled.info = _an_info(activity_id="11")
    await rescheduled.run(run, repaired)

    original_line, rescheduled_line = seen
    assert original_line is not None
    assert rescheduled_line is not None
    assert original_line.attempt.attempt_id != rescheduled_line.attempt.attempt_id
    assert original_line.attempt.conversation_id == rescheduled_line.attempt.conversation_id, "a rewind stays on the same line"


async def test_a_turn_that_cannot_commit_its_output_fails_the_run_rather_than_redelivering(registry: StepRegistry) -> None:
    """An output past the commit ceiling is refused the same way every other continuation refusal is.

    Uncaught it would leave the activity retryable, and every redelivery would build the same
    oversized output again while the run looked merely slow.
    """
    too_large = "the committed output is 2,000,000 bytes, over the 1,000,000 ceiling"

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, output_type, inputs
        raise ContinuationPayloadTooLargeError(too_large)

    host = registry.copy()
    host.perform = perform
    environment = ActivityEnvironment()
    environment.info = _an_info()

    with pytest.raises(ApplicationError) as exc_info:
        await environment.run(
            _inline_step_activity(host),
            InlineStepCall(spec=BROUGHT, step="summarise_sales", arguments={"sales": {"quarter": "Q2", "total": 1000}}, identity=_a_turn()),
        )

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == REFUSED_CONTINUATION
    assert too_large in str(exc_info.value), "what the ledger refused reaches the history"


def test_a_line_belongs_to_one_workflow_execution_one_node_and_one_item() -> None:
    """The three the conversation is derived from, each of them on its own."""
    base = continuation_conversation("execution-a", "digest_each", "3")

    assert base != continuation_conversation("execution-b", "digest_each", "3")
    assert base != continuation_conversation("execution-a", "polish_each", "3")
    assert base != continuation_conversation("execution-a", "digest_each", "9")
    assert base == continuation_conversation("execution-a", "digest_each", "3")
    assert base.version == 5, f"derived in {WORKFLOW_NAMESPACE}"


def test_the_delivery_attempt_is_absent_from_the_logical_attempt_id() -> None:
    """Every component the id is derived from, so a change to one of them is a deliberate one."""
    terms = {
        "workflow_execution": "execution-a",
        "scheduled_activity_id": "5",
        "node_id": "digest_each",
        "item_key": "3",
        "turn_index": 1,
        "logical_retry": 0,
    }
    base = continuation_attempt_id(**terms)

    assert base == continuation_attempt_id(**terms)
    assert len(base) == 64
    assert base != continuation_attempt_id(**{**terms, "scheduled_activity_id": "6"})
    assert base != continuation_attempt_id(**{**terms, "turn_index": 2})
    assert base != continuation_attempt_id(**{**terms, "logical_retry": 1})
    assert base != continuation_attempt_id(**{**terms, "item_key": "9"})
