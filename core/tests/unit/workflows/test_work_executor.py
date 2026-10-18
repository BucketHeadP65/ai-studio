"""Every step invocation funnels through one seam, so a durable engine can own it.

The compiler used to reach `impl.function` from three places. A `StepExecutor` that only saw
two of them would durablize a workflow whose map (or judge) silently ran in-process,
which is the failure this tier pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Tier,
    Workflow,
    run_workflow,
)
from tests.unit.workflows.conftest import ApproveAll, CallLog, Ledger, ReportDraft, ReportUrl, register_quarterly_steps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.registry import StepImplementation

_STRING = Shape(json_schema={"type": "string"})


class Recording:
    """A `StepExecutor` that performs the work here and writes down who asked."""

    def __init__(self) -> None:
        self.performed: list[tuple[str, str, tuple[str, ...]]] = []

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.performed.append((node_id, implementation.name, tuple(sorted(kwargs))))
        return await implementation.function(**kwargs)


class Substituting:
    """A `StepExecutor` that never calls the step -- what a remote engine looks like from here."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        del implementation, kwargs
        return self.answers[node_id]


class Identifying:
    """A `StepExecutor` that reads `CURRENT_STEP` while it is published and writes down what it saw."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None, Tier | None]] = []

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        identity = CURRENT_STEP.get()
        self.seen.append(
            (node_id, identity.node_id if identity is not None else None, identity.tier if identity is not None else None)
        )
        return await implementation.function(**kwargs)


async def test_the_current_step_identity_is_published_for_each_step_call() -> None:
    """Every step call runs under `CURRENT_STEP`, naming its own node and its node's tier -- unset outside a run."""
    registry = StepRegistry()

    async def first(quarter: str) -> str:
        return quarter

    async def second(value: str) -> str:
        return value

    registry.register(first)
    registry.register(second)
    spec = Workflow(
        name="two-steps",
        version=1,
        description="Two chained steps, to prove each call sees its own step identity.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="first", work="first", inputs={"quarter": FromParameter(parameter="quarter")}, tier="helper"),
            RunNode(id="second", work="second", inputs={"value": FromNode(node="first")}),
        ),
        output=FromNode(node="second"),
    )

    assert CURRENT_STEP.get() is None
    executor = Identifying()
    await run_workflow(spec, registry, params={"quarter": "Q2"}, approval=ApproveAll(), executor=executor)
    assert executor.seen == [("first", "first", "helper"), ("second", "second", None)]
    assert CURRENT_STEP.get() is None


async def test_every_run_step_goes_through_the_executor(quarterly_report: Workflow, registry: StepRegistry) -> None:
    executor = Recording()
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=ApproveAll(), executor=executor)
    assert isinstance(result, ReportUrl)
    assert [node_id for node_id, _, _ in executor.performed] == ["fetch_sales", "fetch_costs", "reconcile", "draft_report", "publish"] or [
        node_id for node_id, _, _ in executor.performed
    ] == ["fetch_costs", "fetch_sales", "reconcile", "draft_report", "publish"]
    assert ("reconcile", "reconcile", ("costs", "sales")) in executor.performed


async def test_the_gate_does_not_go_through_the_executor(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A person gate is answered by the `ApprovalHook`, never performed as work."""
    executor = Recording()
    await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=ApproveAll(), executor=executor)
    assert "approve" not in [node_id for node_id, _, _ in executor.performed]


async def test_map_and_judge_steps_go_through_the_executor(call_log: CallLog) -> None:
    """A map's per-item work and a judge's verdict are step calls, so a durable engine must see them."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)

    async def shout(word: str) -> str:
        return word.upper()

    async def is_loud(words: list[str]) -> bool:
        return all(w.isupper() for w in words)

    registry.register(shout)
    registry.register(is_loud)
    spec = Workflow(
        name="shouting",
        version=1,
        description="Shout every word, then check the whole batch is loud.",
        parameters={"words": ParameterSpec(description="Words", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(
            MapNode(id="shouted", turns=("shout",), over=FromParameter(parameter="words"), each="word"),
            JudgeGate(id="loud", work="is_loud", reviews=FromNode(node="shouted"), rejected_to="shouted"),
        ),
        output=FromNode(node="loud"),
    )
    executor = Recording()
    result = await run_workflow(spec, registry, params={"words": ["a", "b"]}, approval=ApproveAll(), executor=executor)
    assert result == ["A", "B"]
    assert [node_id for node_id, _, _ in executor.performed] == ["shouted", "shouted", "loud"]


async def test_a_maps_second_turn_goes_through_the_executor_under_a_derived_id() -> None:
    """A second turn is a second step call per item -- the executor seam must see it separately from the first turn."""

    async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del output_type
        if task.name == "shout":
            return str(inputs["word"]).upper()
        return f"{inputs['shouted']}!"

    registry = StepRegistry(perform=_perform)
    shout = Briefing(
        name="shout",
        instructions="Upper-case the given word, exactly as briefed here, with no other changes to it.",
        inputs={"word": _STRING},
        output=_STRING,
    )
    exclaim = Briefing(
        name="exclaim",
        instructions="Append an exclamation mark to the already-shouted word, exactly as briefed here.",
        inputs={"shouted": _STRING},
        output=_STRING,
    )
    spec = Workflow(
        name="shout-then-exclaim",
        version=1,
        description="Shout each word, then exclaim it -- two turns of one conversation per item.",
        parameters={"words": ParameterSpec(description="Words", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(shout, exclaim),
        nodes=(MapNode(id="shouted", turns=("shout", "exclaim"), over=FromParameter(parameter="words"), each="word"),),
        output=FromNode(node="shouted"),
    )
    executor = Recording()
    result = await run_workflow(spec, registry, params={"words": ["a", "b"]}, approval=ApproveAll(), executor=executor)

    assert result == ["A!", "B!"]
    node_ids = [node_id for node_id, _, _ in executor.performed]
    assert node_ids.count("shouted.shout") == 2, "the first turn, once per item"
    assert node_ids.count("shouted.exclaim") == 2, "the second turn, once per item, under a derived id"
    assert ("shouted.exclaim", "exclaim", ("shouted",)) in executor.performed


async def test_replay_safety_changes_nothing_in_process(quarterly_report: Workflow, call_log: CallLog) -> None:
    """The marker is a durable engine's business alone: `_InProcessExecutor` awaits every step either way.

    A flag that quietly rerouted the in-process path would mean two execution stories for one
    graph, and the one people debug locally would not be the one that ships.
    """
    marked = StepRegistry()
    register_quarterly_steps(marked, call_log, exclude=frozenset({"draft_report"}))

    async def draft_report(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_report")
        return ReportDraft(text=f"net: {ledger.net}")

    marked.register(draft_report, replay_safe=True)
    executor = Recording()
    result = await run_workflow(quarterly_report, marked, params={"quarter": "Q2"}, approval=ApproveAll(), executor=executor)

    assert isinstance(result, ReportUrl)
    assert ("draft_report", "draft_report", ("ledger",)) in executor.performed, "a replay-safe step still goes through the executor seam"
    assert call_log.calls.count("draft_report") == 1

    unmarked_log = CallLog()
    unmarked = StepRegistry()
    register_quarterly_steps(unmarked, unmarked_log)
    plain = Recording()
    await run_workflow(quarterly_report, unmarked, params={"quarter": "Q2"}, approval=ApproveAll(), executor=plain)
    assert [node for node, _, _ in plain.performed] == [node for node, _, _ in executor.performed]


async def test_the_executor_owns_the_answer(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    """What `perform` returns is what the node produced -- the seam a remote engine returns through."""
    executor = Substituting(
        {
            "fetch_sales": None,
            "fetch_costs": None,
            "reconcile": None,
            "draft_report": None,
            "publish": ReportUrl(url="https://elsewhere.example/1"),
        }
    )
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=ApproveAll(), executor=executor)
    assert result == ReportUrl(url="https://elsewhere.example/1")
    assert call_log.calls == [], "no step ran here: the executor answered for all of them"
