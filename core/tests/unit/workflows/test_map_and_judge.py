"""The two verbs the grammar was missing: fan-out over a collection, and a machine-judged gate.

Both were added because the shapes were genuinely unexpressible, not because `pydantic_graph`
happens to support them. `map` could only be faked by writing one node per item, which an artifact
cannot do when the list is produced at runtime by the node above it. A judged gate could be faked
by wiring an agent into `ApprovalHook`, which works and is worse: the artifact then cannot say
whether a person or a machine cleared its publish step, so a saved workflow could have its
approver swapped without a word changing in the file.

What is asserted here is mostly the unglamorous half -- an empty collection terminating, shared
inputs resolved once, a typo'd verdict field refused before anything runs -- because that is where
a fan-out and a branch actually go wrong.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

import pytest

from pydantic import BaseModel, ValidationError

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    GateDecision,
    JudgeGate,
    MapNode,
    ParameterSpec,
    PersonGate,
    RetryExhaustedError,
    RunNode,
    Shape,
    StepIdentity,
    StepRegistry,
    Workflow,
    WorkflowResultSpec,
    WorkflowState,
    build_graph,
    canonical_reviewed_value,
    observed_outcome,
    reviewed_value_digest,
    run_workflow,
    validate,
)
from core.ai.workflows.domain.validator import IssueType
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.exceptions import RepeatedFailureError, UnrecordedTurnError
from core.ai.workflows.graph_builder import _resolve
from tests.unit.workflows.conftest import ApproveAll

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.registry import StepImplementation

_STRING = Shape(json_schema={"type": "string"})


class RunEvidence(BaseModel):
    claim: str
    found: str


class Verdict(BaseModel):
    approved: bool
    why: str


class Weak(BaseModel):
    score: int


@dataclass
class Log:
    """What ran, with what, in order."""

    calls: list[tuple[str, object]] = field(default_factory=list)

    def names(self, name: str) -> list[object]:
        return [args for called, args in self.calls if called == name]


@pytest.fixture
def log() -> Log:
    return Log()


@pytest.fixture
def registry(log: Log) -> StepRegistry:
    """Steps shaped for fan-out: one produces a list, one consumes an item, one consumes the list."""

    async def split_claims(text: str) -> list[str]:
        log.calls.append(("split_claims", text))
        return [part.strip() for part in text.split(",") if part.strip()]

    async def investigate(question: str, depth: str) -> RunEvidence:
        log.calls.append(("investigate", (question, depth)))
        return RunEvidence(claim=question, found=f"{question} at depth {depth}")

    async def summarise(findings: list[RunEvidence]) -> str:
        log.calls.append(("summarise", findings))
        return " | ".join(item.found for item in findings)

    async def judge_evidence(evidence: RunEvidence) -> Verdict:
        log.calls.append(("judge_evidence", evidence))
        return Verdict(approved="good" in evidence.claim, why="contains the word good")

    async def judge_plainly(text: str) -> bool:
        log.calls.append(("judge_plainly", text))
        return "ok" in text

    async def score_it(evidence: RunEvidence) -> Weak:
        log.calls.append(("score_it", evidence))
        return Weak(score=1)

    async def judge_two_things(evidence: RunEvidence, bar: str) -> Verdict:
        log.calls.append(("judge_two_things", (evidence, bar)))
        return Verdict(approved=True, why="")

    async def polish(text: str) -> str:
        log.calls.append(("polish", text))
        return text.upper()

    async def describe(evidence: RunEvidence) -> str:
        log.calls.append(("describe", evidence))
        return evidence.found

    registry = StepRegistry()
    for step in (split_claims, investigate, summarise, judge_evidence, judge_plainly, score_it, judge_two_things, polish, describe):
        registry.register(step)
    return registry


def _map_spec(**overrides: object) -> Workflow:
    """Split a string into claims, investigate each, summarise the collection."""
    node: dict[str, object] = {
        "kind": "map",
        "id": "each_claim",
        "turns": ("investigate",),
        "over": {"node": "claims"},
        "each": "question",
        "inputs": {"depth": {"parameter": "depth"}},
    }
    node.update(overrides)
    return Workflow(
        name="investigate-each-claim",
        version=1,
        description="Fan out over the claims found in one sentence, then summarise what came back.",
        parameters={
            "text": ParameterSpec(description="comma-separated claims", json_schema={"type": "string"}),
            "depth": ParameterSpec(description="how hard to look", json_schema={"type": "string"}),
        },
        nodes=(
            RunNode(id="claims", work="split_claims", inputs={"text": FromParameter(parameter="text")}),
            MapNode.model_validate(node),
            RunNode(id="report", work="summarise", inputs={"findings": FromNode(node="each_claim")}),
        ),
        output=FromNode(node="report"),
    )


# --- map ---------------------------------------------------------------------------------


async def test_a_map_runs_its_step_once_per_item_and_collects_in_order(registry: StepRegistry, log: Log) -> None:
    result = await run_workflow(_map_spec(), registry, params={"text": "alpha, beta, gamma", "depth": "deep"}, approval=ApproveAll())

    assert log.names("investigate") == [("alpha", "deep"), ("beta", "deep"), ("gamma", "deep")]
    assert result == "alpha at depth deep | beta at depth deep | gamma at depth deep"


async def test_a_map_over_nothing_finishes_instead_of_hanging(registry: StepRegistry, log: Log) -> None:
    """The reason `downstream_join_id` is passed to the mapping edge.

    "Investigate every claim you found" legitimately finds none, and a fan-out with no branches has
    no branch to fire the join -- so without telling the mapping edge which join is downstream, this
    run would sit there forever rather than summarising an empty list.
    """
    result = await run_workflow(_map_spec(), registry, params={"text": "  ", "depth": "deep"}, approval=ApproveAll())

    assert log.names("investigate") == []
    assert log.names("summarise") == [[]]
    assert result == ""


async def test_the_shared_inputs_of_a_map_are_resolved_for_every_item(registry: StepRegistry, log: Log) -> None:
    """`depth` is wired once and every item gets it -- only the mapped parameter varies."""
    await run_workflow(_map_spec(), registry, params={"text": "one, two", "depth": "shallow"}, approval=ApproveAll())

    depths = [args[1] for args in log.names("investigate") if isinstance(args, tuple)]
    assert depths == ["shallow", "shallow"]


async def test_a_map_whose_collection_holds_the_wrong_type_is_refused_cold(registry: StepRegistry) -> None:
    """Mapping `list[RunEvidence]` into a step that wants a `str` is a static mistake, so it is static.

    This is the check that makes `map` worth having in a *typed* grammar rather than being a loop
    the runtime discovers: the element type is compared, not merely "is it iterable".
    """
    spec = _map_spec(over={"node": "wrong"})
    spec = spec.model_copy(
        update={
            "nodes": (
                *spec.nodes[:1],
                RunNode(id="wrong", work="summarise", inputs={"findings": FromNode(node="each_claim")}),
                *spec.nodes[1:],
            )
        }
    )
    report = validate(spec, registry)

    assert not report.ok
    kinds = {issue.kind for issue in report.issues}
    assert IssueType.TYPE_MISMATCH in kinds or IssueType.WIRE_CYCLE in kinds


async def test_mapping_over_a_list_of_the_wrong_element_type_is_named_precisely(registry: StepRegistry) -> None:
    spec = Workflow(
        name="bad-map",
        version=1,
        description="Map a step wanting an RunEvidence over a list of strings, which cannot work.",
        parameters={"text": ParameterSpec(description="claims", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="claims", work="split_claims", inputs={"text": FromParameter(parameter="text")}),
            MapNode(id="scored", turns=("score_it",), over=FromNode(node="claims"), each="evidence"),
            RunNode(id="tail", work="polish", inputs={"text": FromParameter(parameter="text")}),
        ),
        output=FromNode(node="tail"),
    )
    report = validate(spec, registry)

    assert not report.ok
    mismatch = [issue for issue in report.issues if issue.kind is IssueType.TYPE_MISMATCH]
    assert mismatch, report.issues
    assert "each item is str" in mismatch[0].detail
    assert "expects RunEvidence" in mismatch[0].detail


async def test_a_map_cannot_wire_the_parameter_it_maps(registry: StepRegistry) -> None:
    """`each` names the parameter fed one item at a time; wiring it too would mean two sources."""
    report = validate(_map_spec(inputs={"depth": {"parameter": "depth"}, "question": {"parameter": "text"}}), registry)

    assert not report.ok
    assert any("It comes from `over`" in issue.detail for issue in report.issues)


async def test_a_map_naming_a_parameter_the_step_does_not_have_is_refused(registry: StepRegistry) -> None:
    report = validate(_map_spec(each="nonexistent"), registry)

    assert not report.ok
    assert any(issue.kind is IssueType.INPUT_MISMATCH and "no parameter 'nonexistent'" in issue.detail for issue in report.issues)


async def test_a_map_that_nothing_reads_is_a_dead_end(registry: StepRegistry) -> None:
    """The dead-end rule covers the new verbs too -- it is about wires, not about node kinds."""
    spec = _map_spec()
    spec = spec.model_copy(
        update={"nodes": (*spec.nodes[:2], RunNode(id="report", work="polish", inputs={"text": FromParameter(parameter="text")}))}
    )
    report = validate(spec, registry)

    assert not report.ok
    assert any(issue.kind is IssueType.DEAD_END_NODE and issue.where == "each_claim" for issue in report.issues)


# --- max_parallel --------------------------------------------------------------------------


async def test_max_parallel_bounds_how_many_items_are_in_flight_at_once() -> None:
    concurrent = 0
    peak = 0

    async def slow(item: str) -> str:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1
        return item

    registry = StepRegistry()
    registry.register(slow)
    spec = Workflow(
        name="bounded-map",
        version=1,
        description="Fan out five items with at most two in flight at once.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="each", turns=("slow",), over=FromParameter(parameter="items"), each="item", max_parallel=2),),
        output=FromNode(node="each"),
    )

    result = await run_workflow(spec, registry, params={"items": ["a", "b", "c", "d", "e"]}, approval=ApproveAll())

    assert result == ["a", "b", "c", "d", "e"]
    assert peak <= 2, "the cap must never be exceeded"
    assert peak == 2, "and it must actually bind, not merely never be exceeded"


async def test_max_parallel_of_none_keeps_todays_unbounded_fan_out(registry: StepRegistry, log: Log) -> None:
    result = await run_workflow(_map_spec(max_parallel=None), registry, params={"text": "a, b, c", "depth": "deep"}, approval=ApproveAll())

    assert len(log.names("investigate")) == 3
    assert result == "a at depth deep | b at depth deep | c at depth deep"


async def test_max_parallel_below_one_is_refused_cold(registry: StepRegistry) -> None:
    report = validate(_map_spec(max_parallel=0), registry)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_MAX_PARALLEL for issue in report.issues)


# --- turns (one worker, many turns of one conversation) ----------------------------------


def _turn(name: str, input_name: str, *, tools: tuple[str, ...] = ()) -> Briefing:
    """One turn: takes a single string parameter and returns a string."""
    return Briefing(
        name=name,
        instructions=f"Do the {name} step on the given text and return the result exactly as briefed here, nothing more.",
        inputs={input_name: _STRING},
        output=_STRING,
        tools=tools,
    )


class _NeverPerformed:
    """A `PerformStep` that must never run -- these tests answer through the `StepExecutor` seam instead."""

    async def __call__(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type, inputs
        msg = f"the recording executor answers for every turn; the briefing {task.name!r} must never run"
        raise AssertionError(msg)


async def test_three_items_and_three_turns_are_nine_calls_on_three_item_keys() -> None:
    """Three items, three turns each: nine performs, one item key per item, shared across its own three calls."""
    digest = _turn("digest", "item", tools=("search",))
    relate = _turn("relate", "digest", tools=("graph",))
    settle = _turn("settle", "relate", tools=("ledger",))

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, StepIdentity | None, dict[str, object]]] = []

        async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
            self.calls.append((node_id, CURRENT_STEP.get(), dict(kwargs)))
            return f"{implementation.name}-out"

    registry = StepRegistry(perform=_NeverPerformed(), grants=("search", "graph", "ledger"))
    spec = Workflow(
        name="three-turns",
        version=1,
        description="Three items, three turns each -- proves the per-item path and its item key.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate, settle),
        nodes=(
            MapNode(id="each", turns=("digest", "relate", "settle"), over=FromParameter(parameter="items"), each="item", tier="helper"),
        ),
        output=FromNode(node="each"),
    )
    executor = Recorder()
    await run_workflow(spec, registry, params={"items": ["a", "b", "c"]}, approval=ApproveAll(), executor=executor)

    assert len(executor.calls) == 9

    by_item_key: dict[str, list[str]] = {}
    for node_id, identity, _ in executor.calls:
        assert identity is not None
        assert identity.item_key is not None
        by_item_key.setdefault(identity.item_key, []).append(node_id)
    assert len(by_item_key) == 3, "one item key per item"
    for node_ids in by_item_key.values():
        assert sorted(node_ids) == ["each.digest", "each.relate", "each.settle"], "every call of one item shares its item key"

    for node_id, identity, _ in executor.calls:
        assert identity is not None
        assert identity.node_id == "each"
        assert identity.tier == "helper", "the map node's own tier reaches every item and every turn"
        facts = identity.turn
        assert facts is not None
        work = node_id.rsplit(".", maxsplit=1)[1]
        assert facts.work == work
        assert facts.index == ("digest", "relate", "settle").index(work)
        assert facts.turns == ("digest", "relate", "settle")
        assert facts.each == "item"
        assert facts.tools == ("graph", "ledger", "search")


async def test_a_later_turn_receives_the_previous_output_as_its_feed() -> None:
    """`relate`'s kwargs carry `digest` -- the value `digest` returned for that same item."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
            self.calls.append((node_id, dict(kwargs)))
            fed = kwargs.get("item", kwargs.get("digest"))
            return f"{implementation.name}({fed})"

    registry = StepRegistry(perform=_NeverPerformed())
    spec = Workflow(
        name="feed",
        version=1,
        description="Two turns over one item, so relate's input is checked against digest's own output.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate),
        nodes=(MapNode(id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="sources"),
    )
    executor = Recorder()
    await run_workflow(spec, registry, params={"items": ["x"]}, approval=ApproveAll(), executor=executor)

    (digest_id, digest_kwargs), (relate_id, relate_kwargs) = executor.calls
    assert digest_id == "sources.digest"
    assert relate_id == "sources.relate"
    assert relate_kwargs["digest"] == f"digest({digest_kwargs['item']})"


async def test_every_turns_outputs_are_kept_and_the_last_is_collected() -> None:
    """`values["sources.digest"]` and `values["sources.relate"]` stay aligned; `values["sources"]` is relate's list."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        if task.name == "digest":
            return f"digest-{inputs['item']}"
        return f"relate-{inputs['digest']}"

    registry = StepRegistry(perform=perform)
    spec = Workflow(
        name="kept-turns",
        version=1,
        description="Two turns over two items, so every turn's own collected list can be checked.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate),
        nodes=(MapNode(id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="sources"),
    )
    state = WorkflowState(params={"items": ["a", "b"]})
    result = await build_graph(spec, registry, approval=ApproveAll()).run(state=state)

    assert result == ["relate-digest-a", "relate-digest-b"]
    assert state.values["sources.digest"] == ["digest-a", "digest-b"]
    assert state.values["sources.relate"] == ["relate-digest-a", "relate-digest-b"]
    assert state.values["sources"] == ["relate-digest-a", "relate-digest-b"]


async def test_a_turn_wire_delivers_an_intermediate_turns_list() -> None:
    """A downstream run node wired `{node: sources, turn: digest}` receives that turn's own list."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        if task.name == "digest":
            return f"digest-{inputs['item']}"
        return f"relate-{inputs['digest']}"

    async def count_digests(digests: list[str]) -> int:
        return len(digests)

    registry = StepRegistry(perform=perform)
    registry.register(count_digests)
    spec = Workflow(
        name="turn-wire",
        version=1,
        description="A downstream node reads an earlier turn's own list, not the map's collected last turn.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate),
        nodes=(
            MapNode(id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item"),
            RunNode(id="tally", work="count_digests", inputs={"digests": FromNode(node="sources", turn="digest")}),
        ),
        output=FromNode(node="tally"),
    )
    result = await run_workflow(spec, registry, params={"items": ["a", "b", "c"]}, approval=ApproveAll())

    assert result == 3


def test_a_turn_wire_that_was_never_recorded_raises_a_named_error() -> None:
    """`_resolve` never returns a bare `KeyError` for a `turn` wire whose value was never recorded."""
    state = WorkflowState(params={})

    with pytest.raises(UnrecordedTurnError) as excinfo:
        _resolve(FromNode(node="sources", turn="digest"), state)

    assert excinfo.value.node_id == "sources"
    assert excinfo.value.turn == "digest"
    assert "sources" in str(excinfo.value)
    assert "digest" in str(excinfo.value)


async def test_a_fully_journaled_multi_turn_map_replays_with_no_executor_calls() -> None:
    """A memo holding the map's own key and every turn's dotted key replays whole -- zero performs."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")
    settle = _turn("settle", "relate")

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
            del kwargs
            self.calls.append(node_id)
            return f"{implementation.name}-out"

    registry = StepRegistry(perform=_NeverPerformed())
    spec = Workflow(
        name="replay-turns",
        version=1,
        description="Three items, three turns, journaled whole -- the memo replays it without executing anything.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate, settle),
        nodes=(MapNode(id="each", turns=("digest", "relate", "settle"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="each", turn="digest"),
    )
    memo = {
        "each": ["settle-a", "settle-b", "settle-c"],
        "each.digest": ["digest-a", "digest-b", "digest-c"],
        "each.relate": ["relate-a", "relate-b", "relate-c"],
        "each.settle": ["settle-a", "settle-b", "settle-c"],
    }
    executor = Recorder()

    result = await run_workflow(
        spec, registry, params={"items": ["a", "b", "c"]}, approval=ApproveAll(), memo=memo, executor=executor
    )

    assert executor.calls == []
    assert result == ["digest-a", "digest-b", "digest-c"]


async def test_a_map_with_a_partial_memo_reruns_every_item_and_every_turn() -> None:
    """A memo missing one turn's dotted key must not replay with a hole -- the whole map reruns."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")
    settle = _turn("settle", "relate")

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, StepIdentity | None]] = []

        async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
            del kwargs
            self.calls.append((node_id, CURRENT_STEP.get()))
            return f"{implementation.name}-out"

    registry = StepRegistry(perform=_NeverPerformed())
    spec = Workflow(
        name="replay-turns-partial",
        version=1,
        description="A memo missing one turn's dotted key must not replay with a hole -- the whole map reruns.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate, settle),
        nodes=(MapNode(id="each", turns=("digest", "relate", "settle"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="each", turn="digest"),
    )
    memo = {
        "each": ["settle-a", "settle-b", "settle-c"],
        "each.digest": ["digest-a", "digest-b", "digest-c"],
        # "each.relate" is missing -- the dropped dotted value that forces a full rerun.
        "each.settle": ["settle-a", "settle-b", "settle-c"],
    }
    executor = Recorder()

    result = await run_workflow(
        spec, registry, params={"items": ["a", "b", "c"]}, approval=ApproveAll(), memo=memo, executor=executor
    )

    assert len(executor.calls) == 9
    by_item_key: dict[str, list[str]] = {}
    for node_id, identity in executor.calls:
        assert identity is not None
        assert identity.item_key is not None
        by_item_key.setdefault(identity.item_key, []).append(node_id)
    assert len(by_item_key) == 3, "one item key per item, even on a full rerun"
    for node_ids in by_item_key.values():
        assert sorted(node_ids) == ["each.digest", "each.relate", "each.settle"]
    assert result == ["digest-out", "digest-out", "digest-out"], "freshly computed, not the stale journaled list"


async def test_a_skipped_item_is_absent_from_every_turns_list() -> None:
    """`on_error: skip`, turn 2 raises for one item -- that item lands in no list, its reason recorded once."""
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        if task.name == "digest":
            return f"digest-{inputs['item']}"
        if inputs["digest"] == "digest-b":
            msg = "relate always breaks on b"
            raise ValueError(msg)
        return f"relate-{inputs['digest']}"

    registry = StepRegistry(perform=perform)
    spec = Workflow(
        name="skip-mid-turn",
        version=1,
        description="Three items; the second one's relate turn breaks and the whole item must be skipped.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate),
        nodes=(
            MapNode(
                id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item", on_error="skip"
            ),
        ),
        output=FromNode(node="sources"),
    )
    state = WorkflowState(params={"items": ["a", "b", "c"]})

    result = await build_graph(spec, registry, approval=ApproveAll()).run(state=state)

    assert result == ["relate-digest-a", "relate-digest-c"]
    assert state.values["sources.digest"] == ["digest-a", "digest-c"]
    assert state.values["sources.relate"] == ["relate-digest-a", "relate-digest-c"]
    assert state.skipped == {"sources": ["relate always breaks on b"]}


async def test_a_single_turn_map_is_unchanged() -> None:
    """One turn: observation ids and `values` stay keyed by `node.id` alone, no dotted keys."""

    class Recorder:
        def __init__(self) -> None:
            self.node_ids: list[str] = []

        async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
            self.node_ids.append(node_id)
            return await implementation.function(**kwargs)

    async def shout(word: str) -> str:
        return word.upper()

    registry = StepRegistry()
    registry.register(shout)
    spec = Workflow(
        name="single-turn",
        version=1,
        description="One turn -- today's shape, unchanged.",
        parameters={"words": ParameterSpec(description="words", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="shouted", turns=("shout",), over=FromParameter(parameter="words"), each="word"),),
        output=FromNode(node="shouted"),
    )
    state = WorkflowState(params={"words": ["a", "b"]})
    executor = Recorder()

    result = await build_graph(spec, registry, approval=ApproveAll(), executor=executor).run(state=state)

    assert result == ["A", "B"]
    assert executor.node_ids == ["shouted", "shouted"]
    assert state.values == {"shouted": ["A", "B"]}
    assert "shouted.shout" not in state.values


async def test_an_empty_collection_still_terminates() -> None:
    digest = _turn("digest", "item")
    relate = _turn("relate", "digest")

    registry = StepRegistry(perform=_NeverPerformed())
    spec = Workflow(
        name="empty-turns",
        version=1,
        description="Mapping nothing must terminate, one turn or many.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest, relate),
        nodes=(MapNode(id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="sources"),
    )
    state = WorkflowState(params={"items": []})

    result = await build_graph(spec, registry, approval=ApproveAll()).run(state=state)

    assert result == []
    assert state.values["sources.digest"] == []
    assert state.values["sources.relate"] == []
    assert state.values["sources"] == []


# --- on_error ------------------------------------------------------------------------------


def _flaky_registry() -> StepRegistry:
    """One step that raises for a fixed item, so a test can pick which item breaks."""

    async def flaky(item: str) -> str:
        if item == "b":
            msg = "b always breaks"
            raise ValueError(msg)
        return item.upper()

    registry = StepRegistry()
    registry.register(flaky)
    return registry


def _flaky_spec(**overrides: object) -> Workflow:
    node: dict[str, object] = {"kind": "map", "id": "each", "turns": ("flaky",), "over": {"parameter": "items"}, "each": "item"}
    node.update(overrides)
    return Workflow(
        name="skip-a-bad-item",
        version=1,
        description="Three items, the middle one raises.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode.model_validate(node),),
        output=FromNode(node="each"),
    )


async def test_skip_leaves_a_failing_item_out_and_the_run_carries_on() -> None:
    @dataclass
    class Watcher:
        events: list[tuple[str, str]] = field(default_factory=list)

        def started(self, node_id: str) -> None:
            self.events.append(("start", node_id))

        def finished(self, node_id: str, *, failed: bool = False) -> None:
            self.events.append(("fail" if failed else "finish", node_id))

    watcher = Watcher()
    state = WorkflowState(params={"items": ["a", "b", "c"]})

    result = await build_graph(_flaky_spec(on_error="skip"), _flaky_registry(), approval=ApproveAll(), observer=watcher).run(
        state=state
    )

    assert result == ["A", "C"]
    assert ("fail", "each") in watcher.events
    assert state.skipped == {"each": ["b always breaks"]}


async def test_on_error_left_at_fail_still_fails_the_run() -> None:
    """The default is unchanged: the first item that raises fails the whole run."""
    with pytest.raises((ValueError, BaseExceptionGroup)):
        await run_workflow(_flaky_spec(), _flaky_registry(), params={"items": ["a", "b", "c"]}, approval=ApproveAll())


async def test_skip_covers_every_turns_failure_too_and_frees_the_shared_slot() -> None:
    """A later turn's failure is skipped the same way, and the shared slot still frees for the next item."""
    events: list[str] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        if task.name == "primary":
            item = inputs["item"]
            events.append(f"primary-start-{item}")
            await asyncio.sleep(0.01)
            events.append(f"primary-end-{item}")
            return str(item)
        fed = inputs["primary"]
        events.append(f"chained-start-{fed}")
        if fed == "a":
            msg = "chained always breaks on a"
            raise ValueError(msg)
        await asyncio.sleep(0.01)
        events.append(f"chained-end-{fed}")
        return str(fed).upper()

    primary = Briefing(
        name="primary",
        instructions="Return the given item unchanged, exactly as briefed here, so a later turn can chain from it.",
        inputs={"item": _STRING},
        output=_STRING,
    )
    chained = Briefing(
        name="chained",
        instructions="Upper-case the primary turn's result exactly as briefed here, with no other change made.",
        inputs={"primary": _STRING},
        output=_STRING,
    )
    registry = StepRegistry(perform=perform)
    spec = Workflow(
        name="skip-a-bad-turn",
        version=1,
        description="Two items sharing one slot; the first item's second turn breaks and is skipped.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(primary, chained),
        nodes=(
            MapNode(
                id="each",
                turns=("primary", "chained"),
                over=FromParameter(parameter="items"),
                each="item",
                max_parallel=1,
                on_error="skip",
            ),
        ),
        output=FromNode(node="each"),
    )
    state = WorkflowState(params={"items": ["a", "b"]})

    result = await build_graph(spec, registry, approval=ApproveAll()).run(state=state)

    assert result == ["B"]
    assert state.skipped == {"each": ["chained always breaks on a"]}
    # item b's primary turn only starts once item a's whole path -- including its failure --
    # is done, which is exactly what releasing the shared slot on a skip looks like.
    assert events.index("primary-start-b") > events.index("chained-start-a")


# --- abort_after_repeats -------------------------------------------------------------------


def _repeats_registry(fails: Mapping[str, type[Exception]]) -> StepRegistry:
    """One step whose failure class is picked per item; any item not named here succeeds."""

    async def flaky(item: str) -> str:
        if (exc_type := fails.get(item)) is not None:
            msg = f"{item} broke"
            raise exc_type(msg)
        return item.upper()

    registry = StepRegistry()
    registry.register(flaky)
    return registry


def _repeats_spec(**overrides: object) -> Workflow:
    """`max_parallel=1` so completion order is item order, and every streak is deterministic."""
    node: dict[str, object] = {
        "kind": "map",
        "id": "each",
        "turns": ("flaky",),
        "over": {"parameter": "items"},
        "each": "item",
        "on_error": "skip",
        "max_parallel": 1,
    }
    node.update(overrides)
    return Workflow(
        name="repeated-failures",
        version=1,
        description="Items whose failure class is picked per item, run one at a time.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode.model_validate(node),),
        output=FromNode(node="each"),
    )


async def test_three_consecutive_same_class_failures_abort_the_run() -> None:
    """`on_error: skip` does not save a systemic failure -- three of the same class ends the run."""
    registry = _repeats_registry({"a": ValueError, "b": ValueError, "c": ValueError})
    state = WorkflowState(params={"items": ["a", "b", "c"]})

    with pytest.raises(RepeatedFailureError) as excinfo:
        await build_graph(_repeats_spec(), registry, approval=ApproveAll()).run(state=state)

    assert excinfo.value.node_id == "each"
    assert excinfo.value.failure == "ValueError"
    assert excinfo.value.count == 3
    assert str(excinfo.value) == "map 'each' aborted: 3 consecutive items failed with ValueError, this is a broken system, not bad items"
    # the third item aborted the run instead of being recorded as a skip -- only the first two,
    # which did not yet reach the ceiling, ever fell through to `on_error: skip`.
    assert state.skipped == {"each": ["a broke", "b broke"]}


async def test_a_success_between_failures_resets_the_streak() -> None:
    """Item b succeeds, so the two `ValueError`s after it never reach the default ceiling of three."""
    registry = _repeats_registry({"a": ValueError, "c": ValueError, "d": ValueError})
    state = WorkflowState(params={"items": ["a", "b", "c", "d"]})

    result = await build_graph(_repeats_spec(), registry, approval=ApproveAll()).run(state=state)

    assert result == ["B"]
    assert state.skipped == {"each": ["a broke", "c broke", "d broke"]}
    assert state.failure_streaks["each"] == ("ValueError", 2)


async def test_alternating_failure_classes_never_accumulate_a_streak() -> None:
    """A different class each time keeps every streak at one, so even a low ceiling never fires."""
    registry = _repeats_registry({"a": ValueError, "b": TypeError, "c": ValueError, "d": TypeError})
    state = WorkflowState(params={"items": ["a", "b", "c", "d"]})

    result = await build_graph(_repeats_spec(abort_after_repeats=2), registry, approval=ApproveAll()).run(state=state)

    assert result == []
    assert state.skipped == {"each": ["a broke", "b broke", "c broke", "d broke"]}
    assert state.failure_streaks["each"] == ("TypeError", 1)


async def test_abort_after_repeats_none_disables_the_ceiling() -> None:
    """`None` keeps today's behaviour: skip is skip, however many items fail the same way in a row."""
    items = ["a", "b", "c", "d", "e"]
    registry = _repeats_registry(dict.fromkeys(items, ValueError))
    state = WorkflowState(params={"items": [*items]})

    result = await build_graph(_repeats_spec(abort_after_repeats=None), registry, approval=ApproveAll()).run(state=state)

    assert result == []
    assert state.skipped == {"each": [f"{item} broke" for item in items]}
    assert "each" not in state.failure_streaks


async def test_an_aborted_map_still_reports_the_items_that_settled_before_the_abort() -> None:
    """The abort cancels the items still in flight, so the outcome is unknown, and every item that did settle is counted."""
    never = asyncio.Event()

    async def flaky(item: str) -> str:
        if item in {"c", "d", "e"}:
            msg = f"{item} broke"
            raise ValueError(msg)
        if item in {"f", "g"}:
            await never.wait()
        return item.upper()

    registry = StepRegistry()
    registry.register(flaky)
    state = WorkflowState(params={"items": ["a", "b", "c", "d", "e", "f", "g"]})

    with pytest.raises(RepeatedFailureError):
        await build_graph(_repeats_spec(), registry, approval=ApproveAll()).run(state=state)
    outcome = observed_outcome(
        WorkflowResultSpec(summary="the upper-cased items", primary_work="each"),
        status="failed",
        facts=list(state.work_facts.values()),
        primary=state.batches["each"],
        decisions=[],
        reviews={},
    )

    assert outcome.status == "unknown"
    assert outcome.primary_input == 7
    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed) == (5, 2, 2, 1)


# --- judge -------------------------------------------------------------------------------


def _judge_spec(**overrides: object) -> Workflow:
    """Investigate one claim, have a step judge it, and only then summarise it."""
    node: dict[str, object] = {
        "kind": "judge",
        "id": "quality",
        "work": "judge_evidence",
        "reviews": {"node": "looked"},
        "approved_when": "approved",
        "rejected_to": "looked",
    }
    node.update(overrides)
    return Workflow(
        name="judged-investigation",
        version=1,
        description="Investigate a claim and let a registered step decide whether it is good enough.",
        parameters={
            "claim": ParameterSpec(description="the claim", json_schema={"type": "string"}),
            "depth": ParameterSpec(description="how hard to look", json_schema={"type": "string"}),
        },
        nodes=(
            RunNode(
                id="looked",
                work="investigate",
                inputs={"question": FromParameter(parameter="claim"), "depth": FromParameter(parameter="depth")},
            ),
            JudgeGate.model_validate(node),
            RunNode(id="report", work="summarise", inputs={"findings": FromNode(node="tail")}),
        ),
        output=FromNode(node="report"),
    )


def _judged(**overrides: object) -> Workflow:
    """The judged spec with its value actually consumed -- `describe` reads it through the judge."""
    spec = _judge_spec(**overrides)
    return spec.model_copy(
        update={
            "nodes": (spec.nodes[0], spec.nodes[1], RunNode(id="report", work="describe", inputs={"evidence": FromNode(node="quality")})),
            "output": FromNode(node="report"),
        }
    )


def _judged_chain(registry: StepRegistry, **overrides: object) -> Workflow:
    """The judged spec, with a list-making tail so `summarise` has something to consume."""
    spec = _judge_spec(**overrides)
    return spec.model_copy(
        update={
            "nodes": (
                *spec.nodes[:2],
                MapNode(id="tail", turns=("score_it",), over=FromNode(node="quality"), each="evidence"),
                RunNode(id="report", work="polish", inputs={"text": FromParameter(parameter="claim")}),
            ),
            "output": FromNode(node="report"),
        }
    )


async def test_a_judge_that_approves_passes_its_value_through(registry: StepRegistry, log: Log) -> None:
    """Approval releases the reviewed value under the judge's own id -- same as a person gate."""
    result = await run_workflow(_judged(), registry, params={"claim": "a good claim", "depth": "deep"}, approval=ApproveAll())

    assert log.names("judge_evidence"), "the judging step never ran"
    assert log.names("investigate") == [("a good claim", "deep")]
    # `describe` read the value through the judge, so approval really did release it.
    assert result == "a good claim at depth deep"


async def test_a_judge_that_rejects_rewinds_and_runs_the_step_again(registry: StepRegistry, log: Log) -> None:
    """A rejection re-runs `rejected_to`, exactly as a human rejection does.

    `judge_evidence` approves only claims containing "good", so a claim without it is rejected --
    and because rejection rewinds to `looked`, the investigation runs a second time. The judge is
    deliberately not idempotent-friendly here: the point is to observe the rewind, and a rewind that
    never terminated would hang this test rather than pass it quietly.
    """
    calls: list[str] = []

    async def investigate_once(question: str, depth: str) -> RunEvidence:
        calls.append(question)
        # First pass looks bad, second looks good, so the rewind converges instead of looping.
        return RunEvidence(claim="bad" if len(calls) == 1 else "good enough", found=f"pass {len(calls)}")

    thin = StepRegistry()
    thin.register(investigate_once)
    for step_name in ("judge_evidence", "describe"):
        implementation = registry.get(step_name)
        assert implementation is not None
        thin.register(implementation.function)
    spec = _judged().model_copy(
        update={
            "nodes": (
                RunNode(
                    id="looked",
                    work="investigate_once",
                    inputs={"question": FromParameter(parameter="claim"), "depth": FromParameter(parameter="depth")},
                ),
                _judged().nodes[1],
                _judged().nodes[2],
            )
        }
    )
    await run_workflow(spec, thin, params={"claim": "anything", "depth": "deep"}, approval=ApproveAll())

    assert len(calls) == 2, f"expected one rejection and one rerun, saw {calls}"


async def test_a_judge_returning_a_bare_bool_needs_no_verdict_field(registry: StepRegistry, log: Log) -> None:
    spec = Workflow(
        name="plainly-judged",
        version=1,
        description="A judge whose step returns a bare bool needs no field naming the verdict.",
        parameters={"claim": ParameterSpec(description="the claim", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="drafted", work="polish", inputs={"text": FromParameter(parameter="claim")}),
            JudgeGate(id="quality", work="judge_plainly", reviews=FromNode(node="drafted"), rejected_to="drafted"),
            RunNode(id="report", work="polish", inputs={"text": FromNode(node="quality")}),
        ),
        output=FromNode(node="report"),
    )
    report = validate(spec, registry)

    assert report.ok, report.issues


async def test_a_judge_whose_verdict_field_does_not_exist_is_refused_cold(registry: StepRegistry) -> None:
    """The typo that would otherwise read as "rejected" on every single run."""
    report = validate(_judged_chain(registry, approved_when="aproved"), registry)

    assert not report.ok
    bad = [issue for issue in report.issues if issue.kind is IssueType.BAD_VERDICT]
    assert bad, report.issues
    assert "has no field 'aproved'" in bad[0].detail


async def test_a_judge_whose_verdict_field_is_not_a_bool_is_refused_cold(registry: StepRegistry) -> None:
    report = validate(_judged_chain(registry, work="score_it", approved_when="score"), registry)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_VERDICT and "not bool" in issue.detail for issue in report.issues)


async def test_a_judge_returning_a_model_must_say_which_field_is_the_verdict(registry: StepRegistry) -> None:
    report = validate(_judged_chain(registry, approved_when=None), registry)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_VERDICT and "set `approved_when`" in issue.detail for issue in report.issues)


async def test_a_judge_needing_two_inputs_is_refused(registry: StepRegistry) -> None:
    """A judge is handed the thing it judges and nothing else; two required inputs is ambiguous."""
    report = validate(_judged_chain(registry, work="judge_two_things"), registry)

    assert not report.ok
    assert any("exactly one required input" in issue.detail for issue in report.issues)


async def test_a_judge_rejecting_to_a_node_it_never_influenced_is_refused(registry: StepRegistry) -> None:
    """The same rule a person gate obeys: a rejection rewinds the gate's own history."""
    report = validate(_judged_chain(registry, rejected_to="report"), registry)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_REJECTED_TO for issue in report.issues)


async def test_a_human_gate_still_behaves_exactly_as_before(registry: StepRegistry) -> None:
    """The judge shares the gate's machinery, so this is the regression guard on the older verb."""
    spec = _judged().model_copy(
        update={
            "nodes": (
                _judged().nodes[0],
                PersonGate(id="quality", reviews=FromNode(node="looked"), rejected_to="looked"),
                _judged().nodes[2],
            )
        }
    )
    report = validate(spec, registry)

    assert report.ok, report.issues


async def test_a_declared_step_can_consume_a_list_of_what_a_step_produces(registry: StepRegistry, log: Log) -> None:
    """`{like: X, many: true}` -- the slot form the map verb made necessary.

    Found by the agent, not by design. Asked for a fan-out whose results were then merged, it wrote
    the map correctly, declared an aggregator to consume the findings, and reported that nothing in
    the grammar could say "a list of what `investigate` produces": `{like: investigate}` names one,
    and re-describing the shape as an `array` builds a different element class and earns a
    `type_mismatch`. A verb whose output nothing can consume only half exists.
    """
    collected: list[object] = []

    async def merge(task: Briefing, output_type: type, /, **inputs: object) -> object:
        collected.append(inputs["findings"])
        return output_type(headline="merged")  # type: ignore[call-arg]

    spec = _map_spec().model_copy(
        update={
            "inline": (
                Briefing(
                    name="merge_findings",
                    instructions="Merge every finding you are given into one headline, keeping each claim's id.",
                    inputs={"findings": SameAs(like="investigate", many=True)},
                    output=Shape(json_schema={"type": "object", "properties": {"headline": {"type": "string"}}, "required": ["headline"]}),
                ),
            ),
            "nodes": (
                _map_spec().nodes[0],
                _map_spec().nodes[1],
                RunNode(id="report", work="merge_findings", inputs={"findings": FromNode(node="each_claim")}),
            ),
        }
    )
    performing = registry.copy()
    performing.perform = merge
    report = validate(spec, performing)
    assert report.ok, report.issues

    await run_workflow(spec, performing, params={"text": "one, two", "depth": "deep"}, approval=ApproveAll())
    assert isinstance(collected[0], list)
    assert [item.claim for item in collected[0]] == ["one", "two"]


async def test_a_type_mismatch_between_two_lists_says_what_is_in_them(registry: StepRegistry) -> None:
    """The refusal that used to read "wire carries list, work expects list" and help nobody."""
    spec = _map_spec().model_copy(
        update={
            "nodes": (
                _map_spec().nodes[0],
                _map_spec().nodes[1],
                RunNode(id="report", work="split_claims", inputs={"text": FromNode(node="each_claim")}),
            )
        }
    )
    report = validate(spec, registry)

    assert not report.ok
    mismatch = [issue for issue in report.issues if issue.kind is IssueType.TYPE_MISMATCH]
    assert mismatch, report.issues
    assert "list[RunEvidence]" in mismatch[0].detail


async def test_a_declared_step_can_consume_one_element_of_a_list_another_declares() -> None:
    """`{like: X, element: true}` -- the inverse of `many`, and the end of the type-carrier smuggle.

    Watched live: an artifact needed a named type for its map's per-item input, and the only way
    to mint one was to declare a step. So the agent wrote a step whose whole existence was its
    output shape -- `"Type carrier for one risk item."` -- was refused for a briefing under 40
    characters, and padded the prose until it passed. The saved artifact carries a step with no
    tools and no purpose, forever. The guardrail was right; the grammar was missing the word.

    The word: a fan-out's per-item worker consumes *one element of* what the list-producer
    declares. Nominal typing is why it must be said by reference -- re-describing the element
    builds a different class -- and `element` extracts the exact class the list carries.
    """
    perform_log: list[str] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        perform_log.append(task.name)
        if task.name == "rank":
            element = get_args(output_type)[0]
            return [element(title="collision"), element(title="drift")]
        return output_type(note=f"settle {inputs['risk'].title}")  # type: ignore[attr-defined, call-arg]

    spec = Workflow(
        name="settle-each-risk",
        version=1,
        description="Rank the risks of one decision, then work out what would settle each one.",
        parameters={"decision": ParameterSpec(description="the decision", json_schema={"type": "string"})},
        inline=(
            Briefing(
                name="rank",
                instructions="Rank the risks this decision carries, most damaging first, one per item.",
                inputs={"decision": Shape(json_schema={"type": "string"})},
                output=Shape(
                    json_schema={
                        "type": "array",
                        "items": {"type": "object", "title": "Risk", "properties": {"title": {"type": "string"}}, "required": ["title"]},
                    }
                ),
            ),
            Briefing(
                name="settle",
                instructions="Say what evidence would settle this one risk, and what to do if it cannot be had.",
                inputs={"risk": SameAs(like="rank", element=True), "decision": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
        ),
        nodes=(
            RunNode(id="risks", work="rank", inputs={"decision": FromParameter(parameter="decision")}),
            MapNode.model_validate(
                {
                    "kind": "map",
                    "id": "per_risk",
                    "turns": ("settle",),
                    "over": {"node": "risks"},
                    "each": "risk",
                    "inputs": {"decision": {"parameter": "decision"}},
                }
            ),
        ),
        output=FromNode(node="per_risk"),
    )
    registry = StepRegistry(perform=perform)
    report = validate(spec, registry)
    assert report.ok, report.issues

    result = await run_workflow(spec, registry, params={"decision": "key by name+version"}, approval=ApproveAll())
    assert perform_log == ["rank", "settle", "settle"], "one rank, then the fan-out"
    assert isinstance(result, list)
    assert [item.note for item in result] == ["settle collision", "settle drift"]


def test_element_of_a_step_that_produces_one_thing_is_refused_by_name() -> None:
    """The refusal a model can act on: it says which step, and what that step actually produces."""
    spec = Workflow(
        name="bad-element",
        version=1,
        description="Take one element of something that is not a list, which cannot mean anything.",
        parameters={"decision": ParameterSpec(description="d", json_schema={"type": "string"})},
        inline=(
            Briefing(
                name="assess",
                instructions="Assess the decision in one paragraph and do not soften any conflict.",
                inputs={"decision": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
            Briefing(
                name="broken",
                instructions="Consume one element of a thing that has no elements, to see the refusal.",
                inputs={"one": SameAs(like="assess", element=True)},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
        ),
        nodes=(RunNode(id="assess", work="assess", inputs={"decision": FromParameter(parameter="decision")}),),
        output=FromNode(node="assess"),
    )

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        msg = "nothing should run"
        raise AssertionError(msg)

    report = validate(spec, StepRegistry(perform=perform))
    assert not report.ok
    complaint = next(issue for issue in report.issues if "element" in issue.detail)
    assert "assess" in complaint.detail, "names the step"
    assert "list" in complaint.detail, "and says what element:true needs"


def test_element_and_many_together_are_refused_as_the_list_they_already_name() -> None:
    """`{like: X, element: true, many: true}` is `{like: X}` wearing two hats; say so."""
    with pytest.raises(ValidationError, match=r"element and many together"):
        SameAs(like="rank", element=True, many=True)


# --- a rejection carries the value it was about into the rerun that must repair it -------------


class RepairVerdict(BaseModel):
    """A judge's own answer: the closed verdict set, what was wrong, and what to change."""

    verdict: Literal["approve", "reject", "cannot_judge"]
    feedback: str = "missing evidence"
    requested_repair: str = "cite the constraint"


_DRAFT_A_DIGEST = "83a8d17cc55d605f287d4a55d300aa60993ea03d7bd3a22a0129f65461a57931"
"""SHA-256 of `"draft A"` as canonical JSON. Pinned: a repair is addressed by this digest."""


def _draft_then_judge(gate: JudgeGate) -> Workflow:
    """One drafting step behind one judge, the judge rejecting back into the draft."""
    return Workflow(
        name="repair-evidence",
        version=1,
        description="A judge sends the draft back with the words it was refused for.",
        parameters={"topic": ParameterSpec(description="What to draft about", json_schema={"type": "string"})},
        nodes=(RunNode(id="draft", work="draft", inputs={"topic": FromParameter(parameter="topic")}), gate),
        output=FromNode(node="judge"),
    )


async def test_a_rerun_reads_the_exact_value_it_was_refused_over_and_the_request_it_started_from() -> None:
    """The second draft is written against draft A and the original topic, not against whatever is newest.

    This is the defect in one line: a rerun that cannot see what was rejected repeats it.
    """
    seen: list[StepIdentity] = []
    registry = StepRegistry()

    async def draft(topic: str) -> str:
        assert topic == "original constraint"
        identity = CURRENT_STEP.get()
        assert identity is not None
        seen.append(identity)
        return "draft A" if len(seen) == 1 else "draft B"

    async def judge(value: str) -> RepairVerdict:
        return RepairVerdict(verdict="approve" if value == "draft B" else "reject")

    registry.register(draft)
    registry.register(judge)
    gate = JudgeGate(id="judge", work="judge", reviews=FromNode(node="draft"), rejected_to="draft", approved_when="verdict")
    state = WorkflowState(params={"topic": "original constraint"})

    assert await build_graph(_draft_then_judge(gate), registry, approval=ApproveAll()).run(state=state) == "draft B"

    assert seen[0].retry_feedback == (), "nothing was refused before the first draft"
    owed = seen[1].retry_feedback[0]
    assert (owed.gate_id, owed.attempt, owed.source) == ("judge", 1, "judge")
    assert owed.feedback == "missing evidence"
    assert owed.requested_repair == "cite the constraint"
    assert owed.reviewed.value_digest == _DRAFT_A_DIGEST
    assert seen[1].reviewed_artifacts is not None
    assert seen[1].reviewed_artifacts.read(owed.reviewed) == "draft A", "the refused draft itself, not the newest one"
    assert seen[1].parameters_json == '{"topic":"original constraint"}'
    assert [(fact.review_generation, fact.verdict, fact.source) for fact in state.gate_decisions] == [
        (1, "reject", "judge"),
        (2, "approve", "judge"),
    ]


@pytest.mark.parametrize("ceiling", [None, 1, 2, 3])
async def test_a_gate_refusing_its_last_reach_ends_the_run_holding_the_whole_repair(ceiling: int | None) -> None:
    """`max_attempts` counts total reaches, the first review included, and the last one stops.

    The error is the only place the unresolved repair can go: the rewind edge is not emitted, so
    nothing downstream ever sees the value the gate refused.
    """
    reaches = ceiling if ceiling is not None else 3
    drafted: list[str] = []
    registry = StepRegistry()

    async def draft(topic: str) -> str:
        assert topic == "original constraint"
        drafted.append("draft A")
        return "draft A"

    async def judge(value: str) -> RepairVerdict:
        assert value == "draft A"
        return RepairVerdict(verdict="reject")

    registry.register(draft)
    registry.register(judge)
    document = {"id": "judge", "work": "judge", "reviews": {"node": "draft"}, "rejected_to": "draft", "approved_when": "verdict"}
    gate = JudgeGate.model_validate(document if ceiling is None else {**document, "max_attempts": ceiling})
    state = WorkflowState(params={"topic": "original constraint"})

    with pytest.raises(RetryExhaustedError) as raised:
        await build_graph(_draft_then_judge(gate), registry, approval=ApproveAll()).run(state=state)

    error = raised.value
    assert (error.gate_id, error.target_node, error.attempts) == ("judge", "draft", reaches)
    assert error.feedback == state.retry_feedback["draft"][-1]
    assert error.feedback.requested_repair == "cite the constraint"
    assert state.reviewed_artifacts.read(error.feedback.reviewed) == "draft A", "the ending holds the value nobody accepted"
    assert len(state.gate_decisions) == reaches
    assert len(drafted) == reaches, "the draft ran once per reach and never once more"


@pytest.mark.parametrize("reviewed_turn", ["draft", None])
async def test_a_maps_repair_reaches_every_item_of_the_refused_turn_and_no_other_node(reviewed_turn: str | None) -> None:
    """Every item of the rerun owes the same repair; a node running the same step owes nothing.

    Keyed by the node the gate reruns rather than by the work it runs, which is what keeps the
    second map -- running the very same `draft` step -- out of a repair nobody asked it for.
    """
    seen: list[tuple[str, StepIdentity]] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        identity = CURRENT_STEP.get()
        assert identity is not None
        seen.append((task.name, identity))
        return str(inputs.get("item", "unrelated"))

    class RefuseOnce:
        """Refuses the first value it is shown and releases the next."""

        reached = 0

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            del gate_id, value, note
            self.reached += 1
            return GateDecision(approve=self.reached > 1, feedback="cite the constraint")

    registry = StepRegistry(perform=perform)
    spec = Workflow(
        name="scoped-map-repair",
        version=1,
        description="A gate over one map's turn, rejecting back into that map alone.",
        parameters={"items": ParameterSpec(description="The items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(
            Briefing(name="draft", instructions="Draft a careful answer from the one item supplied.", inputs={"item": _STRING}, output=_STRING),
            Briefing(name="polish", instructions="Polish the answer against every constraint named.", inputs={"item": _STRING, "text": _STRING}, output=_STRING),
        ),
        nodes=(
            MapNode(id="mapped", turns=("draft", "polish"), over=FromParameter(parameter="items"), each="item"),
            PersonGate(id="review", reviews=FromNode(node="mapped", turn=reviewed_turn), rejected_to="mapped"),
            MapNode(id="unrelated", turns=("draft",), over=FromNode(node="review"), each="item"),
        ),
        output=FromNode(node="unrelated"),
    )

    await build_graph(spec, registry, approval=RefuseOnce()).run(state=WorkflowState(params={"items": ["one", "two"]}))

    repairing = [(work, identity) for work, identity in seen if identity.retry_feedback]
    assert len(repairing) == 2, "one per item of the two-item map"
    assert {work for work, _ in repairing} == {reviewed_turn or "polish"}
    assert {identity.node_id for _, identity in repairing} == {"mapped"}
    assert len({identity.item_key for _, identity in repairing}) == 2, "two items, two item keys"
    assert all(identity.retry_feedback[0].attempt == 1 for _, identity in repairing)
    assert all(not identity.retry_feedback for _, identity in seen if identity.node_id == "unrelated")


@pytest.mark.parametrize("same_bytes", [False, True])
async def test_an_approval_belongs_to_the_generation_it_was_given_over(same_bytes: bool) -> None:
    """A gate re-reached over a second value must be approved again, identical bytes or not.

    `same_bytes` is the sharp case: the rewritten draft happens to come back byte-identical, and
    the digest alone would read as "already approved". The generation is what keeps them apart.
    """
    drafted: list[str] = []
    registry = StepRegistry()

    async def draft() -> str:
        value = "draft A" if not drafted or same_bytes else "draft B"
        drafted.append(value)
        return value

    async def render(text: str) -> str:
        return f"rendered: {text}"

    class WithheldError(RuntimeError):
        """Nobody answers the second generation: the run stops where the question is still open."""

    class Reviewer:
        """Releases the primary once, refuses downstream once, and answers nothing after that."""

        primary_reaches = 0

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            del value, note
            if gate_id != "primary":
                return GateDecision(approve=False, feedback="cite the constraint")
            self.primary_reaches += 1
            if self.primary_reaches > 1:
                raise WithheldError
            return GateDecision(approve=True)

    registry.register(draft)
    registry.register(render)
    spec = Workflow(
        name="generation-approval",
        version=1,
        description="An approval upstream, a rejection downstream, and the upstream asked again.",
        nodes=(
            RunNode(id="draft", work="draft"),
            PersonGate(id="primary", reviews=FromNode(node="draft"), rejected_to="draft"),
            RunNode(id="render", work="render", inputs={"text": FromNode(node="primary")}),
            PersonGate(id="downstream", reviews=FromNode(node="render"), rejected_to="draft"),
        ),
        output=FromNode(node="downstream"),
    )
    state = WorkflowState(params={})

    with pytest.raises(WithheldError):
        await build_graph(spec, registry, approval=Reviewer()).run(state=state)

    primary, downstream = state.gate_decisions
    assert (primary.gate_id, primary.review_generation, primary.verdict) == ("primary", 1, "approve")
    assert primary.reviewed_value_digest == _DRAFT_A_DIGEST
    assert downstream.reviewed_value_digest == reviewed_value_digest(canonical_reviewed_value("rendered: draft A"))
    assert state.gate_generations["primary"] == 2, "the rewind put a second value in front of the same gate"
    assert "primary" not in state.values, "the first approval released nothing for the second generation"
    assert len(state.gate_decisions) == 2, "and no decision was recorded for a generation nobody answered"


@pytest.mark.parametrize("escalate", [False, True])
async def test_a_judge_that_cannot_judge_and_the_person_it_asks_are_two_facts_and_one_reach(escalate: bool) -> None:
    """Neither decision is laundered into the other, and the escalation is not a second attempt."""
    registry = StepRegistry()

    async def draft(topic: str) -> str:
        assert topic == "original constraint"
        return "draft A"

    async def judge(value: str) -> RepairVerdict:
        assert value == "draft A"
        return RepairVerdict(verdict="cannot_judge" if escalate else "approve", feedback="need a person")

    class Person:
        """The escalation's own answer, and a count of how often it was actually asked."""

        asked = 0

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            assert (gate_id, value, note) == ("judge", "draft A", "need a person")
            self.asked += 1
            return GateDecision(approve=True)

    registry.register(draft)
    registry.register(judge)
    gate = JudgeGate(id="judge", work="judge", reviews=FromNode(node="draft"), rejected_to="draft", approved_when="verdict")
    person = Person()
    state = WorkflowState(params={"topic": "original constraint"})

    await build_graph(_draft_then_judge(gate), registry, approval=person).run(state=state)

    assert [(fact.source, fact.verdict) for fact in state.gate_decisions] == (
        [("judge", "cannot_judge"), ("person", "approve")] if escalate else [("judge", "approve")]
    )
    assert all(fact.attempt == fact.review_generation == 1 for fact in state.gate_decisions)
    assert person.asked == int(escalate)


async def test_a_repair_lands_on_the_rerun_targets_own_last_turn_not_the_reviewed_maps() -> None:
    """`rejected_to` need not name the map the gate reviewed, and a turn name is not portable.

    The gate reviews `reviewed_map`'s `draft` turn while its rejection reruns `rerun_map`, which
    has a `draft` turn of its own. Carrying the reviewed turn's name across would put the repair
    on the wrong turn of the wrong map -- silently, since the name resolves on both.
    """
    seen: list[tuple[str, StepIdentity]] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        identity = CURRENT_STEP.get()
        assert identity is not None
        seen.append((f"{identity.node_id}.{task.name}", identity))
        return str(inputs.get("item", "an item"))

    class RefuseOnce:
        """Refuses the first value it is shown and releases the next."""

        reached = 0

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            del gate_id, value, note
            self.reached += 1
            return GateDecision(approve=self.reached > 1, feedback="cite the constraint")

    registry = StepRegistry(perform=perform)
    spec = Workflow(
        name="misaligned-map-repair",
        version=1,
        description="A gate reviewing one map while its rejection reruns another that shares a turn name.",
        parameters={"items": ParameterSpec(description="The items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(
            Briefing(name="draft", instructions="Draft a careful answer from the one item supplied.", inputs={"item": _STRING}, output=_STRING),
            Briefing(name="finish", instructions="Finish the answer against every constraint named.", inputs={"item": _STRING, "text": _STRING}, output=_STRING),
            Briefing(name="polish", instructions="Polish the answer against every constraint named.", inputs={"item": _STRING, "text": _STRING}, output=_STRING),
        ),
        nodes=(
            MapNode(id="rerun_map", turns=("draft", "finish"), over=FromParameter(parameter="items"), each="item"),
            MapNode(id="reviewed_map", turns=("draft", "polish"), over=FromNode(node="rerun_map"), each="item"),
            PersonGate(id="review", reviews=FromNode(node="reviewed_map", turn="draft"), rejected_to="rerun_map"),
        ),
        output=FromNode(node="review"),
    )

    await build_graph(spec, registry, approval=RefuseOnce()).run(state=WorkflowState(params={"items": ["one", "two"]}))

    repairing = sorted(work for work, identity in seen if identity.retry_feedback)
    assert repairing == ["rerun_map.finish", "rerun_map.finish"], "the rerun target's own last turn, once per item"
    assert all(item.target_turn == "finish" for _work, identity in seen for item in identity.retry_feedback)
