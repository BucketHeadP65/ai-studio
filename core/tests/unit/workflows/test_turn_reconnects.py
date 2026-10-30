"""A turn whose model connection breaks starts again from where it began, and only its last failure counts.

The model endpoint can close a streamed answer part way through. The engine then performs the same
turn again, with the same arguments under the same identity, three attempts in all, waiting between
them through the `sleep` a test injects. That holds for every turn the engine runs: a run node, a map
item's turn, a judge, a person gate's answer and an orchestrator gate's decision. A connection that
never opened (phase `pool` or `connect`) and a turn that fails any other way get one attempt, as before.
"""

from __future__ import annotations

import asyncio
import logging

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest

from pydantic import BaseModel
from pydantic_ai.exceptions import ModelConnectionError, ModelTimeoutError, UnexpectedModelBehavior

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    GateDecision,
    JudgeGate,
    MapNode,
    OrchestratorGate,
    ParameterSpec,
    PersonGate,
    RunNode,
    Shape,
    StepIdentity,
    StepRegistry,
    Workflow,
    WorkflowState,
    build_graph,
)
from core.ai.workflows.exceptions import RepeatedFailureError
from tests.unit.workflows.conftest import ApproveAll

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.exceptions import TransportPhase

_STRING = Shape(json_schema={"type": "string"})
_ENGINE_LOGGER = "core.ai.workflows.graph_builder"


def _broken(item: str) -> ModelConnectionError:
    """The error a stream the endpoint closed part way through raises."""
    return ModelConnectionError("test-model", f"{item} lost its connection", phase="read", in_stream=True)


@dataclass
class Breaks:
    """How many leading attempts each item loses, the error each lost attempt raises, and every attempt made, in order."""

    losses: dict[str, int]
    error: Callable[[str], Exception] = _broken
    attempts: list[str] = field(default_factory=list)

    def attempt(self, item: str) -> None:
        self.attempts.append(item)
        if self.attempts.count(item) <= self.losses.get(item, 0):
            raise self.error(item)


@dataclass
class Waits:
    """Records every wait the engine asks for instead of sleeping."""

    seconds: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


@dataclass
class Watcher:
    events: list[tuple[str, str]] = field(default_factory=list)

    def started(self, node_id: str) -> None:
        self.events.append(("start", node_id))

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        self.events.append(("fail" if failed else "finish", node_id))


def _registry(breaks: Breaks) -> StepRegistry:
    async def shout(item: str) -> str:
        breaks.attempt(item)
        return item.upper()

    async def approve_it(text: str) -> bool:
        breaks.attempt(f"judge {text}")
        return True

    registry = StepRegistry()
    registry.register(shout)
    registry.register(approve_it)
    return registry


def _map_spec(**overrides: object) -> Workflow:
    """Items one at a time, so completion order is item order and every streak is deterministic."""
    node: dict[str, object] = {
        "kind": "map",
        "id": "each",
        "turns": ("shout",),
        "over": {"parameter": "items"},
        "each": "item",
        "on_error": "skip",
        "max_parallel": 1,
    }
    node.update(overrides)
    return Workflow(
        name="reconnecting-map",
        version=1,
        description="Items whose turns lose their model connection a chosen number of times.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode.model_validate(node),),
        output=FromNode(node="each"),
    )


def _run_spec() -> Workflow:
    return Workflow(
        name="reconnecting-run",
        version=1,
        description="One run node whose turn loses its model connection a chosen number of times.",
        parameters={"item": ParameterSpec(description="the item", json_schema={"type": "string"})},
        nodes=(RunNode(id="loud", work="shout", inputs={"item": FromParameter(parameter="item")}),),
        output=FromNode(node="loud"),
    )


async def test_an_item_that_loses_its_connection_once_is_collected_and_starts_no_streak(caplog: pytest.LogCaptureFixture) -> None:
    """Each item's first attempt breaks. Counted, the second break would reach the ceiling of two and abort."""
    breaks = Breaks(losses={"a": 1, "b": 1, "c": 1})
    waits = Waits()
    state = WorkflowState(params={"items": ["a", "b", "c"]})

    with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
        result = await build_graph(_map_spec(abort_after_repeats=2), _registry(breaks), approval=ApproveAll(), sleep=waits).run(
            state=state
        )

    assert result == ["A", "B", "C"]
    assert breaks.attempts == ["a", "a", "b", "b", "c", "c"]
    assert waits.seconds == [2.0, 2.0, 2.0]
    assert state.skipped == {}
    assert "each" not in state.failure_streaks
    assert [(fact.disposition, fact.retry_attempts) for fact in state.work_facts.values()] == [("succeeded", 0)] * 3
    first_key = state.batches["each"].items[0]
    assert caplog.records[0].getMessage() == (
        f"'each' item {first_key!r} lost its model connection on attempt 1 of 3, starting the turn again in 2.0 s: "
        "ModelConnectionError: a lost its connection"
    )
    assert [record.levelno for record in caplog.records] == [logging.WARNING] * 3


async def test_three_items_that_lose_every_attempt_still_abort_the_map() -> None:
    """Only the last attempt's failure reaches `on_error` and the streak, so three lost items still end the run."""
    breaks = Breaks(losses={"a": 3, "b": 3, "c": 3})
    waits = Waits()
    state = WorkflowState(params={"items": ["a", "b", "c"]})

    with pytest.raises(RepeatedFailureError) as excinfo:
        await build_graph(_map_spec(), _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=state)

    assert (excinfo.value.node_id, excinfo.value.failure, excinfo.value.count) == ("each", "ModelConnectionError", 3)
    assert breaks.attempts == ["a"] * 3 + ["b"] * 3 + ["c"] * 3
    assert waits.seconds == [2.0, 4.0] * 3
    assert state.skipped == {"each": ["a lost its connection", "b lost its connection"]}


async def test_a_run_node_that_loses_its_connection_once_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """The observer sees one turn that finished, and the node settles once, with no rework."""
    breaks = Breaks(losses={"a": 1})
    waits = Waits()
    watcher = Watcher()
    state = WorkflowState(params={"item": "a"})

    with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
        result = await build_graph(_run_spec(), _registry(breaks), approval=ApproveAll(), observer=watcher, sleep=waits).run(state=state)

    assert result == "A"
    assert breaks.attempts == ["a", "a"]
    assert waits.seconds == [2.0]
    assert watcher.events == [("start", "loud"), ("finish", "loud")]
    fact = state.work_facts[("loud", 0, "")]
    assert (fact.disposition, fact.retry_attempts) == ("succeeded", 0)
    assert [record.getMessage() for record in caplog.records] == [
        "'loud' lost its model connection on attempt 1 of 3, starting the turn again in 2.0 s: ModelConnectionError: a lost its connection"
    ]


async def test_a_judge_that_loses_its_connection_once_still_decides() -> None:
    breaks = Breaks(losses={"judge A": 1})
    waits = Waits()
    spec = Workflow(
        name="reconnecting-judge",
        version=1,
        description="A run node judged by a step whose turn loses its model connection once.",
        parameters={"item": ParameterSpec(description="the item", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="loud", work="shout", inputs={"item": FromParameter(parameter="item")}),
            JudgeGate(id="check", work="approve_it", reviews=FromNode(node="loud"), rejected_to="loud"),
            RunNode(id="again", work="shout", inputs={"item": FromNode(node="check")}),
        ),
        output=FromNode(node="again"),
    )
    state = WorkflowState(params={"item": "a"})

    result = await build_graph(spec, _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=state)

    assert result == "A"
    assert breaks.attempts == ["a", "judge A", "judge A", "A"]
    assert waits.seconds == [2.0]
    assert [(fact.gate_id, fact.verdict, fact.source) for fact in state.gate_decisions] == [("check", "approve", "judge")]


async def test_a_turn_that_fails_another_way_is_not_started_again() -> None:
    """An `UnexpectedModelBehavior` gets one attempt and counts toward the streak exactly as before."""
    breaks = Breaks(losses={"a": 1, "b": 1}, error=lambda item: UnexpectedModelBehavior(f"{item} answered nothing"))
    waits = Waits()
    state = WorkflowState(params={"items": ["a", "b"]})

    with pytest.raises(RepeatedFailureError) as excinfo:
        await build_graph(_map_spec(abort_after_repeats=2), _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=state)

    assert (excinfo.value.failure, excinfo.value.count) == ("UnexpectedModelBehavior", 2)
    assert breaks.attempts == ["a", "b"]
    assert waits.seconds == []


async def test_every_attempt_at_a_turn_starts_from_the_same_inputs_and_identity() -> None:
    """The second turn of an item breaks once. Its next attempt gets the first turn's output again, and the first turn is not redone."""
    seen: list[tuple[str, dict[str, object], StepIdentity | None]] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        seen.append((task.name, dict(inputs), CURRENT_STEP.get()))
        if task.name == "second" and [name for name, _, _ in seen].count("second") == 1:
            raise _broken(task.name)
        return f"{task.name}({next(iter(inputs.values()))})"

    first = Briefing(
        name="first",
        instructions="Name the item you were given, exactly as briefed here, so the next turn can build on it.",
        inputs={"item": _STRING},
        output=_STRING,
    )
    second = Briefing(
        name="second",
        instructions="Build on the first turn's answer exactly as briefed here, with nothing else added to it.",
        inputs={"first": _STRING},
        output=_STRING,
    )
    spec = Workflow(
        name="reconnecting-turns",
        version=1,
        description="One item through two turns of one conversation, the second turn losing its connection once.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(first, second),
        nodes=(MapNode(id="each", turns=("first", "second"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="each"),
    )
    waits = Waits()

    result = await build_graph(spec, StepRegistry(perform=perform), approval=ApproveAll(), sleep=waits).run(
        state=WorkflowState(params={"items": ["a"]})
    )

    assert result == ["second(first(a))"]
    assert [name for name, _, _ in seen] == ["first", "second", "second"]
    (_, broken_inputs, broken_identity), (_, retried_inputs, retried_identity) = seen[1], seen[2]
    assert broken_inputs == retried_inputs == {"first": "first(a)"}
    assert retried_identity == broken_identity
    assert retried_identity is not None
    assert retried_identity.turn is not None
    assert retried_identity.turn.work == "second"
    assert waits.seconds == [2.0]


@pytest.mark.parametrize("phase", ["read", "write", None])
async def test_a_connection_that_broke_after_the_request_left_is_retried_at_every_such_phase(phase: TransportPhase | None) -> None:
    breaks = Breaks(losses={"a": 2}, error=lambda item: ModelConnectionError("test-model", f"{item} broke", phase=phase))
    waits = Waits()

    result = await build_graph(_run_spec(), _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=WorkflowState(params={"item": "a"}))

    assert result == "A"
    assert breaks.attempts == ["a", "a", "a"]
    assert waits.seconds == [2.0, 4.0]


@pytest.mark.parametrize("phase", ["pool", "connect"])
async def test_a_connection_that_never_opened_gets_one_attempt(phase: TransportPhase) -> None:
    """The HTTP client already retried a request that never left, so the engine adds no attempt of its own."""
    breaks = Breaks(losses={"a": 1}, error=lambda item: ModelConnectionError("test-model", f"{item} never connected", phase=phase))
    waits = Waits()

    with pytest.raises(ModelConnectionError) as excinfo:
        await build_graph(_run_spec(), _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=WorkflowState(params={"item": "a"}))

    assert excinfo.value.phase == phase
    assert breaks.attempts == ["a"]
    assert waits.seconds == []


async def test_a_model_timeout_is_retried() -> None:
    breaks = Breaks(losses={"a": 1}, error=lambda item: ModelTimeoutError("test-model", f"{item} timed out", phase="read"))
    waits = Waits()

    result = await build_graph(_run_spec(), _registry(breaks), approval=ApproveAll(), sleep=waits).run(state=WorkflowState(params={"item": "a"}))

    assert result == "A"
    assert breaks.attempts == ["a", "a"]
    assert waits.seconds == [2.0]


async def test_cancelling_a_run_during_the_wait_ends_it_without_another_attempt() -> None:
    breaks = Breaks(losses={"a": 3})
    waiting = asyncio.Event()

    async def wait_for_ever(seconds: float) -> None:
        del seconds
        waiting.set()
        await asyncio.Event().wait()

    graph = build_graph(_run_spec(), _registry(breaks), approval=ApproveAll(), sleep=wait_for_ever)
    run = asyncio.create_task(graph.run(state=WorkflowState(params={"item": "a"})))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)
    assert breaks.attempts == ["a"]


_CHARGE = "Approve the loud item when it is the item upper-cased, and reject it with feedback naming what differs."


def _gated_spec(gate: PersonGate | JudgeGate | OrchestratorGate) -> Workflow:
    """A run node reviewed by `gate`, and a run node reading the value the gate released."""
    return Workflow(
        name="reconnecting-gate",
        version=1,
        description="A run node, a gate whose answer loses its model connection once, and a run node after it.",
        parameters={"item": ParameterSpec(description="the item", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="loud", work="shout", inputs={"item": FromParameter(parameter="item")}),
            gate,
            RunNode(id="again", work="shout", inputs={"item": FromNode(node="check")}),
        ),
        output=FromNode(node="again"),
    )


@dataclass
class BrokenOnce:
    """A gate's answerer whose first answer loses its model connection, then approves every time."""

    asked: list[str] = field(default_factory=list)

    def answer(self, gate_id: str) -> None:
        self.asked.append(gate_id)
        if len(self.asked) == 1:
            raise _broken(gate_id)

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        del value, note
        self.answer(gate_id)
        return GateDecision(approve=True)

    async def decide(self, gate_id: str, charge: str, value: object, /) -> GateDecision:
        del charge, value
        self.answer(gate_id)
        return GateDecision(approve=True)


async def test_an_orchestrator_gate_whose_decision_loses_its_connection_once_still_decides() -> None:
    breaks = Breaks(losses={})
    answerer = BrokenOnce()
    waits = Waits()
    registry = _registry(breaks)
    registry.decide_gate = answerer.decide
    spec = _gated_spec(OrchestratorGate(id="check", charge=_CHARGE, reviews=FromNode(node="loud"), rejected_to="loud"))
    state = WorkflowState(params={"item": "a"})

    result = await build_graph(spec, registry, approval=ApproveAll(), sleep=waits).run(state=state)

    assert result == "A"
    assert answerer.asked == ["check", "check"]
    assert waits.seconds == [2.0]
    assert [(fact.gate_id, fact.verdict, fact.source) for fact in state.gate_decisions] == [("check", "approve", "orchestrator")]


async def test_a_person_gate_whose_answer_loses_its_connection_once_still_decides() -> None:
    """A person gate a host answers with a model, as under orchestrate mode, gets the same three attempts."""
    answerer = BrokenOnce()
    waits = Waits()
    spec = _gated_spec(PersonGate(id="check", reviews=FromNode(node="loud"), rejected_to="loud"))
    state = WorkflowState(params={"item": "a"})

    result = await build_graph(spec, _registry(Breaks(losses={})), approval=answerer, sleep=waits).run(state=state)

    assert result == "A"
    assert answerer.asked == ["check", "check"]
    assert waits.seconds == [2.0]
    assert [(fact.gate_id, fact.verdict, fact.source) for fact in state.gate_decisions] == [("check", "approve", "person")]


class Unsure(BaseModel):
    outcome: Literal["approve", "reject", "cannot_judge"]
    feedback: str


async def test_a_judges_escalation_whose_answer_loses_its_connection_once_still_decides() -> None:
    async def unsure(text: str) -> Unsure:
        del text
        return Unsure(outcome="cannot_judge", feedback="I cannot tell")

    answerer = BrokenOnce()
    waits = Waits()
    registry = _registry(Breaks(losses={}))
    registry.register(unsure)
    spec = _gated_spec(JudgeGate(id="check", work="unsure", reviews=FromNode(node="loud"), approved_when="outcome", rejected_to="loud"))
    state = WorkflowState(params={"item": "a"})

    result = await build_graph(spec, registry, approval=answerer, sleep=waits).run(state=state)

    assert result == "A"
    assert answerer.asked == ["check", "check"]
    assert waits.seconds == [2.0]
    assert [(fact.verdict, fact.source) for fact in state.gate_decisions] == [("cannot_judge", "judge"), ("approve", "person")]
