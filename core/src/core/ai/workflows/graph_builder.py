"""Build a validated workflow into the `pydantic_graph` graph the engine runs.

An invalid workflow is refused before anything is built, and no flag skips the check. Independent nodes fork and
run concurrently, a node with several producers waits behind a join, and a gate becomes a decision node whose
rejected branch reruns the node `rejected_to` names. Node outputs travel through run state keyed by node id. An
approved gate puts the value it reviewed under its own id, so a node wired to the gate reads the approved value.
"""

from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, assert_never

from pydantic import TypeAdapter
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_graph import Graph, GraphBuilder, Join, Step, StepContext
from pydantic_graph.id_types import NodeID

from core.ai.workflows.domain.identity import (
    ReviewedArtifacts,
    StepIdentity,
    TurnFacts,
    canonical_reviewed_value,
    performing_step,
    reviewed_value_digest,
)
from core.ai.workflows.domain.outcome import Disposition, MapBatch, WorkItemFact
from core.ai.workflows.domain.run_events import DecisionSource, GateDecisionFact, RetryFeedback, ReviewedArtifactRef
from core.ai.workflows.domain.validator import prepare, turn_feed
from core.ai.workflows.domain.vocabulary import (
    Briefing,
    FromNode,
    FromParameter,
    InlineStep,
    JudgeGate,
    MapNode,
    Node,
    OrchestratorGate,
    PersonGate,
    RunNode,
    Tier,
    Wire,
    Workflow,
)
from core.ai.workflows.exceptions import RepeatedFailureError, RetryExhaustedError, UnrecordedTurnError, WorkflowInvalidError
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.domain.parameter_values import WorkflowArguments
    from core.ai.workflows.internal_contracts import ApprovalHook, RunObserver, StepExecutor
    from core.ai.workflows.registry import StepImplementation, StepRegistry

_logger = get_logger(__name__)


@dataclass(frozen=True)
class GateDecision:
    """One gate's verdict: whether the value is released, what was wrong with it, and who said so.

    `feedback` and `requested_repair` are read on a rejection and ignored on an approval, which
    releases the value whatever was said alongside it. `source` names the party that actually
    decided. Left `None`, the gate credits the decision to the party it asked.
    """

    approve: bool
    feedback: str = ""
    requested_repair: str = ""
    """What to change. Empty means the feedback is the whole instruction."""
    source: DecisionSource | None = None


@dataclass(frozen=True)
class _Decided:
    """A decision with its reviewer resolved, as the gate records and routes it."""

    approve: bool
    feedback: str
    requested_repair: str
    source: DecisionSource


def _decided(*, decision: GateDecision | bool, asked: DecisionSource) -> _Decided:
    """Read a hook's answer, a `GateDecision` or a bare `bool`, crediting it to `asked` unless it names who decided."""
    if isinstance(decision, bool):
        return _Decided(approve=decision, feedback="", requested_repair="", source=asked)
    return _Decided(
        approve=decision.approve,
        feedback=decision.feedback,
        requested_repair=decision.requested_repair or decision.feedback,
        source=decision.source or asked,
    )


class _InProcessExecutor:
    """The default executor: the step's own callable, awaited in this process, with `implementation.replay_safe` ignored."""

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        del node_id
        return await implementation.function(**kwargs)


class _NoObserver:
    """The default observer: nobody is watching, so both calls do nothing."""

    def started(self, node_id: str) -> None:
        del node_id

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        del node_id, failed


@dataclass
class WorkflowState:
    """Run state: workflow parameters plus every node's output, keyed by node id."""

    params: WorkflowArguments
    """What the run was given, one JSON value per parameter name, already checked against the JSON Schema each
    parameter declares."""
    retry_feedback: dict[str, list[RetryFeedback]] = field(default_factory=dict)
    """The rejections each node owes a repair for, keyed by node id, in the order they were made.

    Keyed by the node the gate reruns, never by the step it runs. Two nodes running the same step
    owe each other nothing, and every item of a map owes the same repair."""
    gate_decisions: list[GateDecisionFact] = field(default_factory=list)
    """Every decision every gate received this run, in the order they arrived."""
    gate_generations: dict[str, int] = field(default_factory=dict)
    """How many values each gate has reviewed, keyed by gate id, counting the one under review now.

    The count is both the attempt checked against the gate's `max_attempts` and the generation a
    decision belongs to. An approval of generation 1 releases nothing for generation 2."""
    reviewed_artifacts: ReviewedArtifacts = field(default_factory=ReviewedArtifacts)
    """This run's reviewed values, captured before each decision. A host may pass a durable
    store of the same shape to keep them after the process ends."""
    values: dict[str, object] = field(default_factory=dict)
    """Every node's output so far, keyed by node id. A multi-turn map also files each turn's list under `<node id>.<turn>`."""
    memo: dict[str, object] = field(default_factory=dict)
    """A previous run's `values`, seeding a resume. Each node's entry is consumed at most once.

    After a resume, a node a gate rewinds to runs fresh, because its entry was spent replaying the
    completed part of the run. A replayed node makes no observer calls."""
    map_semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    """One `Semaphore` per map node with `max_parallel`, created when that map resolves its list.

    Build a fresh `WorkflowState` for every run. A semaphore then never crosses a run or the event
    loop that made it, and two concurrent runs of the same graph each get their own."""
    skipped: dict[str, list[str]] = field(default_factory=dict)
    """The error text of every item `on_error: skip` left out of a map's collected list, keyed by
    map node id, in the order the failures were recorded."""
    batches: dict[str, MapBatch] = field(default_factory=dict)
    """The input each node is working through now and the item keys it minted, keyed by node id.

    A map's batch is minted from the list it resolved, in order. A run node records a one-item batch
    (generation zero, item key `""`), so a run node and a map node answer whether all their work settled in the same way."""
    work_facts: dict[tuple[str, int, str], WorkItemFact] = field(default_factory=dict)
    """How each piece of work ended, keyed by `(node id, generation, item key)`.

    A rewind onto the same key replaces its entry rather than adding a second one. Entries of a
    generation the input has moved past stay as a record."""
    rewound: set[str] = field(default_factory=set)
    """The value keys a rejection sent the run back through that have not been written again.

    `values` keeps them, so a branch still running can read them.
    A host that saves `values` for a later resume leaves them out."""
    failure_streaks: dict[str, tuple[str, int]] = field(default_factory=dict)
    """Each map's current streak of item failures of one exception class, as `(class name, count)`
    keyed by map node id. Any item that succeeds clears it."""


_SINGLE_ITEM = MapBatch(generation=0, ordered_digest="", items=("",))
"""The input of a node that does its work exactly once: one piece, under the empty item key."""

_NOT_IN_RUN_LOG = object()
"""Sentinel: `_replay` found no entry, so the node must actually execute."""

_SKIPPED = object()
"""Sentinel for an item that failed under `on_error: skip`. `_collect` and `_gather` drop it, so it never leaves this module."""


@dataclass(frozen=True)
class _MappedItem:
    """One item travelling the mapping edge under the key its fan-out minted for it."""

    key: str
    value: object


def _settle(state: WorkflowState, node_id: str, generation: int, item_key: str, disposition: Disposition, detail: str = "") -> None:
    """File how one piece of work ended, replacing any entry of the same identity and counting that as one more retry."""
    identity = (node_id, generation, item_key)
    standing = state.work_facts.get(identity)
    state.work_facts[identity] = WorkItemFact(
        node_id=node_id,
        generation=generation,
        item_key=item_key,
        disposition=disposition,
        retry_attempts=0 if standing is None else standing.retry_attempts + 1,
        detail=detail,
    )


def _open_batch(state: WorkflowState, node_id: str, rows: list[object]) -> MapBatch:
    """The item keys `rows` mints for `node_id`: the same generation when the rows are unchanged in order, else the next."""
    digests = [reviewed_value_digest(canonical_reviewed_value(row)) for row in rows]
    ordered = reviewed_value_digest(canonical_reviewed_value(digests))
    standing = state.batches.get(node_id)
    same = standing is not None and standing.ordered_digest == ordered and len(standing.items) == len(rows)
    generation = standing.generation if standing is not None and same else 0 if standing is None else standing.generation + 1
    batch = MapBatch(
        generation=generation,
        ordered_digest=ordered,
        items=tuple(f"{generation}:{index}:{digest[:16]}" for index, digest in enumerate(digests)),
    )
    state.batches[node_id] = batch
    return batch


def _record_value(state: WorkflowState, key: str, value: object) -> None:
    """Write a value key and clear its rewound mark."""
    state.values[key] = value
    state.rewound.discard(key)


def _replay(node_id: str, state: WorkflowState) -> object:
    """Consume the memo's entry for this node and write it into run state, or return `_NOT_IN_RUN_LOG`."""
    if node_id not in state.memo:
        return _NOT_IN_RUN_LOG
    value = state.memo.pop(node_id)
    _record_value(state, node_id, value)
    return value


@dataclass(frozen=True)
class _Approved:
    value: object


@dataclass(frozen=True)
class _Rejected:
    feedback: RetryFeedback


@dataclass(frozen=True)
class _BudgetStop:
    """A judge that spent its budget before answering. Read as cannot judge, with the stop as its note."""

    note: str


_WorkflowGraph = Graph[WorkflowState, None, None, object]
_Builder = GraphBuilder[WorkflowState, None, None, object]
_RunStep = Step[WorkflowState, None, object, object]
_GateStep = Step[WorkflowState, None, object, "_Approved | _Rejected"]
_NodeStep = _RunStep | _GateStep
_Entry = _NodeStep | Join[WorkflowState, None, object, None]
"""Where an edge into a node lands: the node's step, or the join guarding it."""


async def _observed[R](node_id: str, watching: RunObserver, work: Awaitable[R]) -> R:
    """Run one node's work between the observer's `started` and `finished` calls, calling `finished` on failure too."""
    watching.started(node_id)
    try:
        result = await work
    except BaseException:
        watching.finished(node_id, failed=True)
        raise
    watching.finished(node_id)
    return result


def _await_all(_current: None, _inputs: object) -> None:
    """Join reducer that only waits for every producer branch. Values travel through run state, not the join."""
    return


def _collect(current: list[object], inputs: object) -> list[object]:
    """Join reducer for a map: append each item's output as it arrives, leaving out `_SKIPPED`."""
    # Appending in place is safe only while the join passes `initial_factory=list`, which gives each run its own list.
    if inputs is not _SKIPPED:
        current.append(inputs)
    return current


def build_graph(
    workflow: Workflow,
    registry: StepRegistry,
    *,
    approval: ApprovalHook,
    observer: RunObserver | None = None,
    executor: StepExecutor | None = None,
) -> _WorkflowGraph:
    """Validate `workflow`, then build it into a graph the engine can run.

    Args:
        workflow: The workflow to build. It must validate against `registry`.
        registry: The registry of steps this run can perform. The workflow's inline steps are added to a
            copy of it, so the registry passed in is never changed. The graph binds to that copy, the same
            registry validation checked the wiring against.
        approval: Who answers each person gate, and each judge gate whose judge cannot judge.
        observer: Told when each node starts and finishes. None means nobody watches.
        executor: How each step is performed. None awaits the step in this process.

    Returns:
        The built graph. Run it with a fresh `WorkflowState`.

    Raises:
        WorkflowInvalidError: If validation finds any issue. Nothing is built from a workflow that fails it.
    """
    prepared = prepare(workflow, registry)
    if not prepared.report.ok:
        raise WorkflowInvalidError(prepared.report)
    registry = prepared.registry

    watching = observer if observer is not None else _NoObserver()
    performing = executor if executor is not None else _InProcessExecutor()
    builder: _Builder = GraphBuilder(name=workflow.name, state_type=WorkflowState, output_type=object)
    # `heads` holds where an edge into a node lands and where a rejection rewinds to. `exits` holds where
    # its outgoing edges leave from. They differ only for a `map`, which expands into four graph nodes:
    # producers arrive at the first of them and consumers leave from the last.
    heads: dict[str, _NodeStep] = {}
    exits: dict[str, _NodeStep] = {}
    gate_steps: dict[str, _GateStep] = {}
    inline: Mapping[str, InlineStep] = {task.name: task for task in workflow.inline}
    for node in workflow.nodes:
        match node:
            case RunNode():
                heads[node.id] = exits[node.id] = _build_run_step(builder, node, registry, watching, performing)
            case PersonGate():
                gate_steps[node.id] = _build_gate_step(builder, node, approval, watching)
                heads[node.id] = exits[node.id] = gate_steps[node.id]
            case JudgeGate():
                gate_steps[node.id] = _build_judge_step(builder, node, registry, watching, performing, approval)
                heads[node.id] = exits[node.id] = gate_steps[node.id]
            case OrchestratorGate():
                gate_steps[node.id] = _build_orchestrator_step(builder, node, registry, watching)
                heads[node.id] = exits[node.id] = gate_steps[node.id]
            case MapNode():
                heads[node.id], exits[node.id] = _build_map_steps(builder, node, registry, watching, performing, inline)
            case _ as unreachable:
                assert_never(unreachable)

    async def _collect_output(ctx: StepContext[WorkflowState, None, object]) -> object:
        return _resolve(workflow.output, ctx.state)

    collect = builder.step(_collect_output, node_id="__output")
    builder.add(builder.edge_from(collect).to(builder.end_node))

    _emit_edges(builder, workflow, heads, exits, gate_steps, collect)
    return builder.build()


async def run_workflow(
    workflow: Workflow,
    registry: StepRegistry,
    *,
    params: WorkflowArguments,
    approval: ApprovalHook,
    observer: RunObserver | None = None,
    memo: Mapping[str, object] | None = None,
    executor: StepExecutor | None = None,
) -> object:
    """Build `workflow` and run it once in this process.

    Args:
        workflow: The workflow to run. It must validate against `registry`.
        registry: The registry of steps this run can perform.
        params: The run's parameter values, already checked against the workflow's parameters.
        approval: Who answers each person gate, and each judge gate whose judge cannot judge.
        observer: Told when each node starts and finishes. None means nobody watches.
        memo: A previous run's recorded node outputs, to resume from. Each node it holds replays
            its output instead of running again.
        executor: How each step is performed. None awaits the step in this process.

    Returns:
        The value the workflow's `output` wire names.

    Raises:
        WorkflowInvalidError: If validation finds any issue. Nothing runs.
        RetryExhaustedError: If a gate rejects its last permitted attempt.
        RepeatedFailureError: If a map's items fail with one exception class `abort_after_repeats` times in a row.
    """
    graph = build_graph(workflow, registry, approval=approval, observer=observer, executor=executor)
    return await graph.run(state=WorkflowState(params=params, memo=dict(memo or {})))


def _emit_edges(
    builder: _Builder,
    workflow: Workflow,
    heads: Mapping[str, _NodeStep],
    exits: Mapping[str, _NodeStep],
    gate_steps: Mapping[str, _GateStep],
    collect: _RunStep,
) -> None:
    """Add the workflow's edges: a fork to several consumers, a join where a node waits on several producers, a decision after each gate."""
    producers = {node.id: _producer_ids(node) for node in workflow.nodes}
    consumers: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}
    for node in workflow.nodes:  # workflow order everywhere, so one workflow always builds the same graph
        for producer in producers[node.id]:
            consumers[producer].append(node.id)

    entries: dict[str, _Entry] = {}
    for node in workflow.nodes:
        entries[node.id] = heads[node.id]
        if len(producers[node.id]) > 1:
            join = builder.join(_await_all, initial=None, node_id=f"{node.id}__await_all")
            builder.add(builder.edge_from(join).to(heads[node.id]))
            entries[node.id] = join

    roots = [heads[node.id] for node in workflow.nodes if not producers[node.id]]
    builder.add(builder.edge_from(builder.start_node).to(roots[0], *roots[1:]))

    output_node = workflow.output.node if isinstance(workflow.output, FromNode) else None
    for node in workflow.nodes:
        outs: list[_Entry | _RunStep] = [entries[consumer] for consumer in consumers[node.id]]
        if node.id == output_node:
            outs.append(collect)
        if isinstance(node, PersonGate | JudgeGate | OrchestratorGate):
            # A rejection rewinds to the node's HEAD, not its entry join: the producers upstream of
            # it have already run, and re-entering a join would sit there waiting for branches that
            # will never fire again.
            verdict = (
                builder.decision(node_id=f"{node.id}__verdict")
                .branch(builder.match(_Approved).to(outs[0], *outs[1:]))
                .branch(
                    builder.match(_Rejected).to(
                        _build_retry_step(
                            builder, node, workflow, heads[node.rejected_to], _rewound_keys(workflow, node.rejected_to, consumers)
                        )
                    )
                )
            )
            builder.add(builder.edge_from(gate_steps[node.id]).to(verdict))
        elif outs:
            builder.add(builder.edge_from(exits[node.id]).to(outs[0], *outs[1:]))


def _implementation(registry: StepRegistry, work: str) -> StepImplementation:
    """The step a node binds to. Validation ran first, so a miss raises `RuntimeError` as a bug in this module."""
    impl = registry.get(work)
    if impl is None:
        msg = f"work {work!r} is not in the registry this spec was validated against: build_graph validates before it builds"
        raise RuntimeError(msg)
    return impl


def _build_run_step(builder: _Builder, node: RunNode, registry: StepRegistry, watching: RunObserver, performing: StepExecutor) -> _RunStep:
    impl = _implementation(registry, node.work)
    # A parameter wire's value is validated at run time with the step's own input type.
    # Each adapter is built once, when the graph is built.
    adapters: dict[str, TypeAdapter[object]] = {
        name: TypeAdapter(impl.input_types[name]) for name, wire in node.inputs.items() if isinstance(wire, FromParameter)
    }

    async def _run(ctx: StepContext[WorkflowState, None, object]) -> object:
        if (hit := _replay(node.id, ctx.state)) is not _NOT_IN_RUN_LOG:
            return hit
        ctx.state.batches[node.id] = _SINGLE_ITEM
        kwargs: dict[str, object] = {}
        for name, wire in node.inputs.items():
            value = _resolve(wire, ctx.state)
            if (adapter := adapters.get(name)) is not None:
                value = adapter.validate_python(value)
            kwargs[name] = value
        try:
            with performing_step(_step_identity(node.id, ctx.state, tier=node.tier)):
                output = await _observed(node.id, watching, performing.perform(node.id, impl, kwargs))
        except Exception as exc:
            _settle(ctx.state, node.id, 0, "", "failed", f"{type(exc).__qualname__}: {exc}")
            raise
        _settle(ctx.state, node.id, 0, "", "succeeded")
        _record_value(ctx.state, node.id, output)
        return output

    return builder.step(_run, node_id=node.id)


def _claim_whole_run_log(node: MapNode, state: WorkflowState) -> bool:
    """Whether the memo holds all of this map's entries, its own key and each `<id>.<turn>`, dropping a partial set."""
    keys = (node.id,) if len(node.turns) == 1 else (node.id, *(f"{node.id}.{work}" for work in node.turns))
    if all(key in state.memo for key in keys):
        return True
    for key in keys:
        state.memo.pop(key, None)
    return False


def _build_map_steps(
    builder: _Builder,
    node: MapNode,
    registry: StepRegistry,
    watching: RunObserver,
    performing: StepExecutor,
    inline: Mapping[str, InlineStep],
) -> tuple[_RunStep, _RunStep]:
    """Add a map's four graph nodes (resolve the list, run each item through every turn, collect, gather), return the first and last."""
    impls = [_implementation(registry, work) for work in node.turns]
    feeds = [turn_feed(impl, node, first=index == 0) for index, impl in enumerate(impls)]
    shared: list[dict[str, TypeAdapter[object]]] = [
        {
            name: TypeAdapter(impl.input_types[name])
            for name, wire in node.inputs.items()
            if isinstance(wire, FromParameter) and name in impl.input_types
        }
        for impl in impls
    ]
    multi = len(node.turns) > 1
    declared = [task for work in node.turns if isinstance((task := inline.get(work)), Briefing)] if multi else []
    turn_tools = tuple(sorted({tool for task in declared for tool in task.tools}))
    turn_skills = tuple(sorted({skill for task in declared for skill in task.skills}))
    turn_outputs = tuple(impl.output_type for impl in impls) if multi else ()
    fork_id = NodeID(f"{node.id}__fork")
    join_id = NodeID(f"{node.id}__collect")

    async def _over(ctx: StepContext[WorkflowState, None, object]) -> list[object]:
        if node.max_parallel is not None:
            # Created here, where it runs once before the fan-out, so every item finds the same
            # semaphore. Replacing one an earlier rewind left is safe, because that fan-out's
            # join already fired and nothing waits on it.
            ctx.state.map_semaphores[node.id] = asyncio.Semaphore(node.max_parallel)
        if _claim_whole_run_log(node, ctx.state):
            # The whole map is in the run log: fan out nothing and let `_gather` replay the
            # recorded list. `_gather` pops the entries, not this step.
            return []
        value = _resolve(node.over, ctx.state)
        # A list wired from a workflow parameter is first seen here. A `str` is refused although it
        # is iterable, because mapping over a sentence's characters is never meant.
        if isinstance(value, str) or not isinstance(value, Iterable):
            msg = f"map {node.id!r} needs a collection to map over, but {node.over} produced {type(value).__name__}"
            raise TypeError(msg)
        rows = list(value)
        batch = _open_batch(ctx.state, node.id, rows)
        return [_MappedItem(key=key, value=row) for key, row in zip(batch.items, rows, strict=True)]

    async def _run_item(ctx: StepContext[WorkflowState, None, object]) -> object:
        item = ctx.inputs
        if not isinstance(item, _MappedItem):  # pragma: no cover (the mapping edge carries only what `_over` returned)
            msg = f"map {node.id!r} was handed {type(item).__name__} instead of one of its own items"
            raise TypeError(msg)
        generation, item_key = ctx.state.batches[node.id].generation, item.key
        outputs: list[object] = []
        previous: object = None
        try:
            for index, (work, impl) in enumerate(zip(node.turns, impls, strict=True)):
                kwargs: dict[str, object] = {}
                if node.each in impl.input_types:
                    kwargs[node.each] = item.value
                for name, wire in node.inputs.items():
                    if name not in impl.input_types:
                        continue
                    value = _resolve(wire, ctx.state)
                    if (adapter := shared[index].get(name)) is not None:
                        value = adapter.validate_python(value)
                    kwargs[name] = value
                if (feed := feeds[index]) is not None:
                    kwargs[feed] = previous
                # With several turns, each turn is observed under its own `<id>.<turn>` id so a
                # watcher can tell them apart.
                observed_id = f"{node.id}.{work}" if multi else node.id
                facts = (
                    TurnFacts(
                        work=work,
                        index=index,
                        turns=node.turns,
                        each=node.each,
                        tools=turn_tools,
                        outputs=turn_outputs,
                        skills=turn_skills,
                    )
                    if multi
                    else None
                )
                with performing_step(_step_identity(node.id, ctx.state, work=work, item_key=item_key, turn=facts, tier=node.tier)):
                    previous = await _observed(observed_id, watching, performing.perform(observed_id, impl, kwargs))
                outputs.append(previous)
        except Exception as exc:
            # `_observed` already reported this item as failed. `on_error` decides only what the
            # collected list does with the failure. `BaseException` (`CancelledError` above all)
            # is not caught, so a cancelled run still stops every sibling item.
            failure = type(exc).__qualname__
            if node.abort_after_repeats is not None:
                streak, count = ctx.state.failure_streaks.get(node.id, (failure, 0))
                count = count + 1 if streak == failure else 1
                ctx.state.failure_streaks[node.id] = (failure, count)
                if count >= node.abort_after_repeats:
                    _settle(ctx.state, node.id, generation, item_key, "failed", f"{failure} repeated {count} times")
                    raise RepeatedFailureError(node.id, failure, count) from exc
            reason = str(exc)
            first_line = reason.splitlines()[0] if reason else type(exc).__name__
            if node.on_error != "skip":
                _settle(ctx.state, node.id, generation, item_key, "failed", f"{failure}: {first_line}")
                raise
            _logger.warning("map {node_id!r} skipped an item: {reason}", node_id=node.id, reason=first_line)
            ctx.state.skipped.setdefault(node.id, []).append(reason)
            _settle(ctx.state, node.id, generation, item_key, "skipped", first_line)
            return _SKIPPED
        ctx.state.failure_streaks.pop(node.id, None)
        _settle(ctx.state, node.id, generation, item_key, "succeeded")
        return tuple(outputs) if multi else previous

    async def _each(ctx: StepContext[WorkflowState, None, object]) -> object:
        semaphore = ctx.state.map_semaphores.get(node.id)
        if semaphore is None:
            return await _run_item(ctx)
        # One slot covers every turn of an item, so a bounded map never has more than
        # `max_parallel` items in flight at once.
        async with semaphore:
            return await _run_item(ctx)

    async def _gather(ctx: StepContext[WorkflowState, None, object]) -> object:
        if _claim_whole_run_log(node, ctx.state):
            hit = _replay(node.id, ctx.state)
            if multi:
                for work in node.turns:
                    _replay(f"{node.id}.{work}", ctx.state)
            return hit
        rows = [item for item in ctx.inputs if item is not _SKIPPED] if isinstance(ctx.inputs, list) else []
        if not multi:
            _record_value(ctx.state, node.id, rows)
            return rows
        for index, work in enumerate(node.turns):
            _record_value(ctx.state, f"{node.id}.{work}", [row[index] for row in rows])
        collected = [row[-1] for row in rows]
        _record_value(ctx.state, node.id, collected)
        return collected

    over = builder.step(_over, node_id=f"{node.id}__over")
    each = builder.step(_each, node_id=f"{node.id}__each")
    joined = builder.join(_collect, initial_factory=list, node_id=join_id, parent_fork_id=fork_id)
    gathered = builder.step(_gather, node_id=node.id)
    # Without `downstream_join_id`, a map over an empty list hangs instead of reaching the join.
    builder.add_mapping_edge(over, each, fork_id=fork_id, downstream_join_id=join_id)
    builder.add(builder.edge_from(each).to(joined))
    builder.add(builder.edge_from(joined).to(gathered))
    return over, gathered


def _step_identity(node_id: str, state: WorkflowState, *, work: str | None = None,
                   item_key: str | None = None, turn: TurnFacts | None = None, tier: Tier | None = None) -> StepIdentity:
    """What this call is performed for, with the node's retry feedback that names this turn or no turn."""
    owed = tuple(item for item in state.retry_feedback.get(node_id, ()) if item.target_turn is None or item.target_turn == work)
    return StepIdentity(node_id=node_id, item_key=item_key, turn=turn, tier=tier, retry_feedback=owed,
                        parameters_json=canonical_reviewed_value(state.params) if owed else "{}",
                        reviewed_artifacts=state.reviewed_artifacts)


def _begin_review(node: PersonGate | JudgeGate | OrchestratorGate, state: WorkflowState, value: object) -> ReviewedArtifactRef:
    """Open one review at `node`: count it, withdraw the value an earlier approval released, and capture this one."""
    state.gate_generations[node.id] = state.gate_generations.get(node.id, 0) + 1
    state.values.pop(node.id, None)
    wire = node.reviews
    node_id = wire.node if isinstance(wire, FromNode) else f"parameter:{wire.parameter}"
    return state.reviewed_artifacts.persist(node_id, wire.turn if isinstance(wire, FromNode) else None, value)


def _record_decision(gate_id: str, state: WorkflowState, reviewed: ReviewedArtifactRef, *,
                     verdict: Literal["approve", "reject", "cannot_judge"], source: DecisionSource) -> None:
    """File one decision as a fact: which reach, which generation, who decided, over which bytes."""
    generation = state.gate_generations[gate_id]
    state.gate_decisions.append(
        GateDecisionFact(gate_id=gate_id, attempt=generation, review_generation=generation, verdict=verdict,
                         source=source, reviewed_value_digest=reviewed.value_digest)
    )


def _finish_review(node: PersonGate | JudgeGate | OrchestratorGate, state: WorkflowState, value: object,
                   reviewed: ReviewedArtifactRef, decided: _Decided) -> _Approved | _Rejected:
    """Record the decision, then release the value or return the rejection. A rejection credited to `auto` raises `ValueError`."""
    source = decided.source
    if decided.approve:
        _record_decision(node.id, state, reviewed, verdict="approve", source=source)
        _record_value(state, node.id, value)
        return _Approved(value)
    if source == "auto":
        msg = f"gate {node.id!r} was refused by automode, which decides nothing. A rejection must name its reviewer"
        raise ValueError(msg)
    _record_decision(node.id, state, reviewed, verdict="reject", source=source)
    return _Rejected(
        RetryFeedback(gate_id=node.id, source=source, target_node=node.rejected_to, attempt=state.gate_generations[node.id],
                      feedback=decided.feedback, reviewed=reviewed, requested_repair=decided.requested_repair)
    )


def _rewound_keys(workflow: Workflow, target: str, consumers: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The value keys a rejection back to `target` invalidates: `target`, everything downstream of it, and each multi-turn map's turn keys."""
    by_id = {node.id: node for node in workflow.nodes}
    seen: dict[str, None] = {target: None}
    queue = [target]
    while queue:
        for consumer in consumers[queue.pop(0)]:
            if consumer not in seen:
                seen[consumer] = None
                queue.append(consumer)
    keys: list[str] = []
    for node_id in seen:
        keys.append(node_id)
        node = by_id[node_id]
        if isinstance(node, MapNode) and len(node.turns) > 1:
            keys.extend(f"{node_id}.{work}" for work in node.turns)
    return tuple(keys)


def _build_retry_step(
    builder: _Builder, node: PersonGate | JudgeGate | OrchestratorGate, workflow: Workflow, target: _NodeStep, rewound: Sequence[str]
) -> _RunStep:
    """Add a gate's rejected branch: file the repair, mark the rewound values, then rewind to the target or raise `RetryExhaustedError`."""
    target_node = next(item for item in workflow.nodes if item.id == node.rejected_to)
    target_turn: str | None = None
    if isinstance(target_node, MapNode):
        reviews = node.reviews
        named = reviews.turn if isinstance(reviews, FromNode) and reviews.node == target_node.id else None
        target_turn = named or target_node.turns[-1]

    async def _retry(ctx: StepContext[WorkflowState, None, object]) -> object:
        rejected = ctx.inputs
        if not isinstance(rejected, _Rejected):  # pragma: no cover (the decision edge matches `_Rejected` alone)
            msg = f"the rejected branch of gate {node.id!r} was handed {type(rejected).__name__}"
            raise TypeError(msg)
        feedback = rejected.feedback.model_copy(update={"target_turn": target_turn})
        ctx.state.retry_feedback.setdefault(node.rejected_to, []).append(feedback)
        if feedback.attempt >= node.max_attempts:
            raise RetryExhaustedError(node.id, node.rejected_to, feedback.attempt, feedback)
        ctx.state.rewound.update(rewound)
        return feedback

    retry = builder.step(_retry, node_id=f"{node.id}__retry")
    builder.add(builder.edge_from(retry).to(target))
    return retry


def _build_judge_step(
    builder: _Builder, node: JudgeGate, registry: StepRegistry, watching: RunObserver, performing: StepExecutor, approval: ApprovalHook
) -> _GateStep:
    """Add a gate a judge step decides, like a person gate. A `cannot_judge` verdict asks `approval`, with the judge's note."""
    impl = _implementation(registry, node.work)
    (parameter,) = impl.required - node.inputs.keys()
    # Parameter wires are runtime-parsed with the step's own type, same as a run node's inputs.
    adapters: dict[str, TypeAdapter[object]] = {
        name: TypeAdapter(impl.input_types[name]) for name, wire in node.inputs.items() if isinstance(wire, FromParameter)
    }

    async def _judge(ctx: StepContext[WorkflowState, None, object]) -> _Approved | _Rejected:
        if (hit := _replay(node.id, ctx.state)) is not _NOT_IN_RUN_LOG:
            return _Approved(hit)  # only an approval records a pass-through, a rejection leaves nothing
        value = _resolve(node.reviews, ctx.state)
        reviewed = _begin_review(node, ctx.state, value)
        wired: dict[str, object] = {}
        for name, wire in node.inputs.items():
            wired_value = _resolve(wire, ctx.state)
            if (adapter := adapters.get(name)) is not None:
                wired_value = adapter.validate_python(wired_value)
            wired[name] = wired_value
        async def _decide() -> object:
            """The judge's answer, or the budget stop as an answer: running out never fails the run."""
            try:
                return await performing.perform(node.id, impl, {parameter: value, **wired})
            except UsageLimitExceeded as exc:
                which = str(exc).split(".")[0]
                return _BudgetStop(f"The reviewer ran out of budget before it could decide. {which}.")

        with performing_step(StepIdentity(node_id=node.id)):
            verdict = await _observed(node.id, watching, _decide())
        if isinstance(verdict, _BudgetStop):
            outcome: object = "cannot_judge"
            note = verdict.note
        else:
            outcome = verdict if node.approved_when is None else getattr(verdict, node.approved_when)
            note = str(getattr(verdict, "feedback", ""))
        repair = getattr(verdict, "requested_repair", "")
        decided = _Decided(approve=outcome is True or outcome == "approve", feedback=note,
                           requested_repair=(repair if isinstance(repair, str) else "") or note, source="judge")
        if outcome == "cannot_judge":
            # Two facts, one reach: the judge's own refusal to answer, then whoever answered in
            # its place. The escalation is not a second attempt at this gate.
            _record_decision(node.id, ctx.state, reviewed, verdict="cannot_judge", source="judge")
            decided = _decided(decision=await _observed(node.id, watching, approval.review(node.id, value, note=note)), asked="person")
        return _finish_review(node, ctx.state, value, reviewed, decided)

    return builder.step(_judge, node_id=node.id)


def _build_orchestrator_step(builder: _Builder, node: OrchestratorGate, registry: StepRegistry, watching: RunObserver) -> _GateStep:
    """Add a gate the host's orchestrator decides, with a person gate's outcomes, pass-through and rewind."""
    decide = registry.decide_gate
    if decide is None:
        msg = (
            f"gate {node.id!r} needs an orchestrator and this registry has none: "
            f"build_graph validates before it builds, so this spec should never have reached here"
        )
        raise RuntimeError(msg)

    async def _orchestrate(ctx: StepContext[WorkflowState, None, object]) -> _Approved | _Rejected:
        if (hit := _replay(node.id, ctx.state)) is not _NOT_IN_RUN_LOG:
            return _Approved(hit)  # only an approval records a pass-through, a rejection leaves nothing
        value = _resolve(node.reviews, ctx.state)
        reviewed = _begin_review(node, ctx.state, value)
        # Observed like the other gates: the gate is "running" for as long as the orchestrator takes.
        decided = _decided(decision=await _observed(node.id, watching, decide(node.id, node.charge, value)), asked="orchestrator")
        return _finish_review(node, ctx.state, value, reviewed, decided)

    return builder.step(_orchestrate, node_id=node.id)


def _build_gate_step(builder: _Builder, node: PersonGate, approval: ApprovalHook, watching: RunObserver) -> _GateStep:
    async def _gate(ctx: StepContext[WorkflowState, None, object]) -> _Approved | _Rejected:
        if (hit := _replay(node.id, ctx.state)) is not _NOT_IN_RUN_LOG:
            return _Approved(hit)  # the recorded approval releases its value and nobody is asked again
        value = _resolve(node.reviews, ctx.state)
        reviewed = _begin_review(node, ctx.state, value)
        # A gate is "running" for exactly as long as a human takes.
        decided = _decided(decision=await _observed(node.id, watching, approval.review(node.id, value)), asked="person")
        return _finish_review(node, ctx.state, value, reviewed, decided)

    return builder.step(_gate, node_id=node.id)


def _resolve(wire: Wire, state: WorkflowState) -> object:
    match wire:
        case FromParameter(parameter=name):
            return state.params[name]
        case FromNode(node=node_id, turn=turn):
            if turn is None:
                return state.values[node_id]
            key = f"{node_id}.{turn}"
            if key not in state.values:
                raise UnrecordedTurnError(node_id, turn)
            return state.values[key]
        case _ as unreachable:
            assert_never(unreachable)


def _producer_ids(node: Node) -> list[str]:
    """This node's upstream node ids, deduplicated, in input order. Must read the same wires `domain.validator.wires_of` reads."""
    wires: list[Wire]
    match node:
        case RunNode():
            wires = list(node.inputs.values())
        case JudgeGate():
            wires = [node.reviews, *node.inputs.values()]
        case PersonGate() | OrchestratorGate():
            wires = [node.reviews]
        case MapNode():
            wires = [node.over, *node.inputs.values()]
        case _ as unreachable:
            assert_never(unreachable)
    seen: dict[str, None] = {}
    for wire in wires:
        if isinstance(wire, FromNode):
            seen.setdefault(wire.node)
    return list(seen)
