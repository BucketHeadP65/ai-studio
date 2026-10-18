"""A gate's wired inputs, and the rewind that has to re-run them.

`reviews` was once the only wire a gate contributed a scheduling edge for, so a gate input wired
from another node passed `validate` and then had no edge in the compiled graph: the producer was
a dead end pydantic_graph refused, or, when something else happened to read it too, a node the
gate could overtake. Both authorities now read the same wires.

Giving a gate several producers gives it a wait. A wait is only sound when a rejection re-runs
every branch it reads, because one that re-runs only part of the fan leaves the wait unsatisfiable.
The validator checks that. A wait whose branches straddle a map's fan-out runs as written.

Every run here counts performances per node. A wait released early fires its consumer twice while
the order of events and the final value both still look right, so a test that checks only those is
blind to the fault it is meant to catch.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    GateDecision,
    JudgeGate,
    MapNode,
    OrchestratorGate,
    ParameterSpec,
    PersonGate,
    Program,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    build_graph,
    run_workflow,
    validate,
)
from core.ai.workflows.domain.validator import IssueType, wires_of
from core.ai.workflows.graph_builder import _producer_ids
from tests.unit.workflows.conftest import ApproveAll

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import Program as CodeSpecType
    from core.ai.workflows.registry import StepImplementation

CHARGE = "Decide whether the traced plan is safe to act on, and say plainly what would make it unsafe."


@dataclass
class Counting:
    """A `StepExecutor` that counts performances per node id, then performs the step itself.

    Per node rather than per step, so one step serving both arms of a fan still reads as one
    performance each. `counts` is the assertion surface: a wait released early runs its consumer
    twice, and nothing else about the run looks wrong when it does.
    """

    counts: dict[str, int] = field(default_factory=dict)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.counts[node_id] = self.counts.get(node_id, 0) + 1
        return await implementation.function(**kwargs)


# --- the wires a gate contributes a producer edge for ------------------------------------------


def test_a_judge_contributes_an_edge_for_its_inputs_as_well_as_what_it_reviews() -> None:
    gate = JudgeGate(
        id="judged",
        work="check",
        reviews=FromNode(node="drafted"),
        inputs={"source": FromNode(node="resolved"), "collection": FromParameter(parameter="collection")},
        rejected_to="drafted",
    )

    assert _producer_ids(gate) == ["drafted", "resolved"], "a parameter wire has no producer; both node wires do"


def test_a_judge_naming_one_node_twice_still_has_one_producer() -> None:
    gate = JudgeGate(
        id="judged", work="check", reviews=FromNode(node="drafted"), inputs={"echo": FromNode(node="drafted")}, rejected_to="drafted"
    )

    assert _producer_ids(gate) == ["drafted"], "one producer, so no wait to build"


def test_a_human_gate_and_an_orchestrator_gate_contribute_the_wire_they_have() -> None:
    """Neither kind takes `inputs` at all, so `reviews` is the whole of what they can name."""
    human = PersonGate(id="approved", reviews=FromNode(node="drafted"), rejected_to="drafted")
    orchestrator = OrchestratorGate(id="cleared", charge=CHARGE, reviews=FromNode(node="drafted"), rejected_to="drafted")

    assert _producer_ids(human) == ["drafted"]
    assert _producer_ids(orchestrator) == ["drafted"]
    assert not hasattr(human, "inputs")
    assert not hasattr(orchestrator, "inputs")


def test_every_gate_kind_contributes_exactly_what_the_validator_walks() -> None:
    """The invariant behind the bug: the two authorities must name the same wires, gate by gate."""
    gates = (
        JudgeGate(
            id="judged", work="check", reviews=FromNode(node="drafted"), inputs={"source": FromNode(node="resolved")}, rejected_to="drafted"
        ),
        PersonGate(id="approved", reviews=FromNode(node="drafted"), rejected_to="drafted"),
        OrchestratorGate(id="cleared", charge=CHARGE, reviews=FromNode(node="drafted"), rejected_to="drafted"),
    )

    for gate in gates:
        walked = list(dict.fromkeys(wire.node for _, wire in wires_of(gate) if isinstance(wire, FromNode)))
        assert _producer_ids(gate) == walked, gate.id


# --- the glue task whose only consumer is a gate's input ---------------------------------------


@dataclass
class Sandbox:
    """A `CodeRunner` that answers the one program these tests bring, recording when it ran.

    `order` is shared with the registered steps, so the whole run reads as one sequence.
    """

    order: list[str]

    async def __call__(self, step: CodeSpecType, output_type: type, /, **inputs: object) -> object:
        del output_type
        self.order.append(step.name)
        return str(inputs["plan"]).removeprefix("plan for ")


_STRING = Shape(json_schema={"type": "string"})

_SOURCE_ID_OF = Program(
    name="source_id_of",
    source='plan.removeprefix("plan for ")',
    inputs={"plan": _STRING},
    output=_STRING,
)


def _glue_spec() -> Workflow:
    """The shape that used to validate and then refuse to compile.

    `resolved` is read by nothing but the judge's wired input, so before gates contributed an
    edge for their inputs it was a node with no outgoing edges.
    """
    return Workflow(
        name="glue-into-a-gate",
        version=1,
        description="A brought program resolves an id, and the only thing that reads it is a judge's wired input.",
        parameters={"source": ParameterSpec(description="the source to trace", json_schema={"type": "string"})},
        inline=(_SOURCE_ID_OF,),
        nodes=(
            RunNode(id="trace", work="trace_removal", inputs={"source": FromParameter(parameter="source")}),
            RunNode(id="resolved", work="source_id_of", inputs={"plan": FromNode(node="trace")}),
            JudgeGate(
                id="judged",
                work="check_excision",
                reviews=FromNode(node="trace"),
                inputs={"source": FromNode(node="resolved")},
                rejected_to="trace",
            ),
        ),
        output=FromNode(node="judged"),
    )


@dataclass
class GlueRun:
    """The registry and the running record for the glue spec, built together."""

    registry: StepRegistry
    order: list[str]
    judged_with: list[dict[str, object]]


def _glue_run() -> GlueRun:
    order: list[str] = []
    judged_with: list[dict[str, object]] = []

    async def trace_removal(source: str) -> str:
        order.append("trace_removal")
        return f"plan for {source}"

    async def check_excision(plan: str, source: str) -> bool:
        order.append("check_excision")
        judged_with.append({"plan": plan, "source": source})
        return True

    registry = StepRegistry(run_code=Sandbox(order=order))
    registry.register(trace_removal)
    registry.register(check_excision)
    return GlueRun(registry=registry, order=order, judged_with=judged_with)


def test_a_glue_task_read_only_by_a_gate_input_now_compiles() -> None:
    run = _glue_run()

    report = validate(_glue_spec(), run.registry)
    assert report.ok, report.issues

    build_graph(_glue_spec(), run.registry, approval=ApproveAll())


async def test_a_glue_task_read_only_by_a_gate_input_runs_before_the_gate() -> None:
    run = _glue_run()

    result = await run_workflow(_glue_spec(), run.registry, params={"source": "src-1"}, approval=ApproveAll())

    assert result == "plan for src-1", "the judge approved, so its reviewed value passes through"
    assert run.order == ["trace_removal", "source_id_of", "check_excision"], "the glue is scheduled ahead of the gate that reads it"
    assert run.judged_with == [{"plan": "plan for src-1", "source": "src-1"}]


# --- the rewind that has to re-run every branch a wait reads -----------------------------------


def _fan_registry() -> StepRegistry:
    async def base() -> str:
        return "b"

    async def left(text: str) -> str:
        return text + "L"

    async def right(text: str) -> str:
        return text + "R"

    async def combine(a: str, b: str) -> str:
        return a + b

    async def judge_two(drafts: str, extra: str) -> bool:
        return True

    async def judge_one(drafts: str) -> bool:
        return True

    registry = StepRegistry()
    for step in (base, left, right, combine, judge_two, judge_one):
        registry.register(step)
    return registry


_DIAMOND = (
    RunNode(id="base", work="base"),
    RunNode(id="left", work="left", inputs={"text": FromNode(node="base")}),
    RunNode(id="right", work="right", inputs={"text": FromNode(node="base")}),
)


def _diamond_spec(*nodes: object, output: str) -> Workflow:
    return Workflow.model_validate(
        {
            "name": "diamond",
            "version": 1,
            "description": "One node splits into two arms and something downstream waits on both of them.",
            "nodes": (*_DIAMOND, *nodes),
            "output": {"node": output},
        }
    )


def _judge_over_both(rejected_to: str) -> JudgeGate:
    return JudgeGate(
        id="judged", work="judge_two", reviews=FromNode(node="left"), inputs={"extra": FromNode(node="right")}, rejected_to=rejected_to
    )


async def test_a_gate_waiting_on_both_arms_runs_once_when_the_rewind_governs_both() -> None:
    spec = _diamond_spec(_judge_over_both("base"), output="judged")
    registry = _fan_registry()
    executor = Counting()

    assert validate(spec, registry).ok

    result = await run_workflow(spec, registry, params={}, approval=ApproveAll(), executor=executor)

    assert result == "bL"
    assert executor.counts == {"base": 1, "left": 1, "right": 1, "judged": 1}


def test_a_gate_waiting_on_both_arms_is_refused_when_its_rewind_re_runs_only_one() -> None:
    spec = _diamond_spec(_judge_over_both("left"), output="judged")

    report = validate(spec, _fan_registry())

    assert not report.ok
    partial = [issue for issue in report.issues if issue.kind is IssueType.PARTIAL_REWIND]
    assert partial, report.issues
    assert partial[0].where == "judged.rejected_to", "the fix is to move this gate's rewind, so that is what it names"
    assert partial[0].detail == (
        "a rejection at gate 'judged' rewinds to 'left', and 'judged' would still be waiting on 'right'. A rewind "
        "must re-run every branch a waiting node reads, so point rejected_to at a node that every path to those "
        "producers passes through."
    )


def test_a_gate_waiting_on_two_roots_is_refused_because_no_rewind_can_govern_both() -> None:
    """Independent roots split at the start of the run, and `rejected_to` can only name a real node."""
    spec = Workflow.model_validate(
        {
            "name": "two-roots",
            "version": 1,
            "description": "A judge reviews one root and takes a second, unrelated root as a wired input.",
            "nodes": (
                RunNode(id="base", work="base"),
                RunNode(id="other", work="base"),
                JudgeGate(
                    id="judged", work="judge_two", reviews=FromNode(node="base"), inputs={"extra": FromNode(node="other")}, rejected_to="base"
                ),
            ),
            "output": {"node": "judged"},
        }
    )

    report = validate(spec, _fan_registry())

    assert [issue.kind for issue in report.issues] == [IssueType.PARTIAL_REWIND]
    assert "would still be waiting on 'other'" in report.issues[0].detail


def test_a_gate_further_downstream_rewinding_into_the_fan_is_refused_too() -> None:
    """The wait belongs to `judged`, but any gate below it can rewind past that wait."""
    spec = _diamond_spec(
        _judge_over_both("base"),
        JudgeGate(id="second", work="judge_one", reviews=FromNode(node="judged"), rejected_to="left"),
        output="second",
    )

    report = validate(spec, _fan_registry())

    partial = [issue for issue in report.issues if issue.kind is IssueType.PARTIAL_REWIND]
    assert partial, report.issues
    assert partial[0].where == "second.rejected_to"
    assert "'judged' would still be waiting on 'right'" in partial[0].detail


def test_a_run_node_waiting_on_both_arms_is_refused_on_the_same_terms() -> None:
    """The wait is the compiler's, not the gate's, so a plain run node's fan is judged the same way."""
    spec = _diamond_spec(
        RunNode(id="written", work="combine", inputs={"a": FromNode(node="left"), "b": FromNode(node="right")}),
        JudgeGate(id="second", work="judge_one", reviews=FromNode(node="written"), rejected_to="left"),
        output="second",
    )

    report = validate(spec, _fan_registry())

    partial = [issue for issue in report.issues if issue.kind is IssueType.PARTIAL_REWIND]
    assert partial, report.issues
    assert partial[0].where == "second.rejected_to"
    assert "'written' would still be waiting on 'right'" in partial[0].detail


async def test_a_rewind_landing_below_the_wait_leaves_it_alone() -> None:
    """A gate that rewinds to a node under the wait never re-enters it, so there is nothing to refuse."""
    spec = _diamond_spec(
        _judge_over_both("base"),
        RunNode(id="after", work="left", inputs={"text": FromNode(node="judged")}),
        JudgeGate(id="second", work="judge_one", reviews=FromNode(node="after"), rejected_to="after"),
        output="second",
    )
    registry = _fan_registry()
    executor = Counting()

    assert validate(spec, registry).ok

    result = await run_workflow(spec, registry, params={}, approval=ApproveAll(), executor=executor)

    assert result == "bLL"
    assert executor.counts == {"base": 1, "left": 1, "right": 1, "judged": 1, "after": 1, "second": 1}


# --- a wait across a map's fan-out ------------------------------------------------------------


async def test_a_wait_on_a_gate_behind_a_map_and_a_plain_sibling_fires_once_after_the_gate() -> None:
    """A node waits on a judge that reviews a map's collected output and on a sibling root.

    The ordering is forced rather than slept on: the sibling starts only once the map's list has
    landed, and the map's items finish only after the sibling has. Both waits are then open when
    the map collects, and the waiting node must still hold until the judge has approved.
    """
    listed = asyncio.Event()
    numbered = asyncio.Event()
    order: list[str] = []

    async def listing() -> list[str]:
        listed.set()
        return ["1", "2", "3"]

    async def numbering() -> str:
        await asyncio.wait_for(listed.wait(), timeout=5.0)
        numbered.set()
        return "n"

    async def per_item(item: str) -> str:
        await asyncio.wait_for(numbered.wait(), timeout=5.0)
        await asyncio.sleep(0.01)
        return "i" + item

    async def judge_a_list(drafts: list[str]) -> bool:
        order.append("check")
        return True

    async def act_on(judged: list[str], plan: str) -> str:
        order.append("written")
        return f"{plan}/{','.join(judged)}"

    registry = StepRegistry()
    for step in (listing, numbering, per_item, judge_a_list, act_on):
        registry.register(step)

    spec = Workflow.model_validate(
        {
            "name": "gate-behind-a-map-and-a-sibling",
            "version": 1,
            "description": "A node waits on a judge over a map's output and on a sibling root of that map.",
            "nodes": (
                RunNode(id="pending", work="listing"),
                RunNode(id="numbered", work="numbering"),
                MapNode(id="drafts", turns=("per_item",), over=FromNode(node="pending"), each="item"),
                JudgeGate(id="check", work="judge_a_list", reviews=FromNode(node="drafts"), rejected_to="drafts"),
                RunNode(id="written", work="act_on", inputs={"judged": FromNode(node="check"), "plan": FromNode(node="numbered")}),
            ),
            "output": {"node": "written"},
        }
    )
    executor = Counting()

    report = validate(spec, registry)
    assert report.ok, report.issues

    result = await asyncio.wait_for(run_workflow(spec, registry, params={}, approval=ApproveAll(), executor=executor), timeout=10.0)

    assert result == "n/i1,i2,i3"
    assert order == ["check", "written"], "the waiting node runs only after the judge has approved"
    assert executor.counts == {"pending": 1, "numbered": 1, "drafts": 3, "check": 1, "written": 1}


async def test_a_plan_upstream_of_the_map_fires_once_under_the_forced_ordering() -> None:
    """An approved plan upstream of a judged map feeds the acting node beside the judge's release.

    The ordering is forced rather than slept on: the map's items block until the approval that
    also feeds the wait has landed. This is the shape `wf.source-remove.v1` is built on.
    """
    approval_landed = asyncio.Event()

    async def trace() -> str:
        return "plan"

    async def relist(text: str) -> list[str]:
        approval_landed.set()
        return [text + n for n in ("1", "2", "3")]

    async def per_item(item: str) -> str:
        await asyncio.wait_for(approval_landed.wait(), timeout=5.0)
        await asyncio.sleep(0.01)
        return "i" + item

    async def judge_a_list(drafts: list[str]) -> bool:
        return True

    async def act_on(judged: list[str], plan: str) -> str:
        return f"{plan}/{len(judged)}"

    registry = StepRegistry()
    for step in (trace, relist, per_item, judge_a_list, act_on):
        registry.register(step)

    spec = Workflow.model_validate(
        {
            "name": "plan-beside-a-judged-map",
            "version": 1,
            "description": "An approved plan feeds a map, and the acting node waits on that approval and the judged map.",
            "nodes": (
                RunNode(id="traced", work="trace"),
                PersonGate(id="owner_approves", reviews=FromNode(node="traced"), rejected_to="traced"),
                RunNode(id="items", work="relist", inputs={"text": FromNode(node="owner_approves")}),
                MapNode(id="excisions", turns=("per_item",), over=FromNode(node="items"), each="item"),
                JudgeGate(id="check", work="judge_a_list", reviews=FromNode(node="excisions"), rejected_to="excisions"),
                RunNode(id="mechanical", work="act_on", inputs={"judged": FromNode(node="check"), "plan": FromNode(node="owner_approves")}),
            ),
            "output": {"node": "mechanical"},
        }
    )
    executor = Counting()

    assert validate(spec, registry).ok, validate(spec, registry).issues

    result = await asyncio.wait_for(
        run_workflow(spec, registry, params={}, approval=ApproveAll(), executor=executor), timeout=10.0
    )

    assert result == "plan/3"
    assert executor.counts == {"traced": 1, "items": 1, "excisions": 3, "check": 1, "mechanical": 1}


async def test_an_orchestrator_gate_takes_the_same_wait_as_the_others() -> None:
    """Only a judge can carry `inputs`, so an orchestrator gate reaches a wait through a gate producer."""
    decided: list[str] = []

    async def base() -> str:
        return "b"

    async def left(text: str) -> str:
        return text + "L"

    async def judge_two(drafts: str, extra: str) -> bool:
        return True

    async def decide(gate_id: str, charge: str, value: object, /) -> GateDecision:
        del charge, value
        decided.append(gate_id)
        return GateDecision(approve=True)

    registry = StepRegistry(decide_gate=decide)
    for step in (base, left, judge_two):
        registry.register(step)

    spec = Workflow.model_validate(
        {
            "name": "orchestrator-behind-a-wait",
            "version": 1,
            "description": "An orchestrator gate clears one arm and a judge waits on that clearance and the other arm.",
            "nodes": (
                RunNode(id="base", work="base"),
                RunNode(id="arm", work="left", inputs={"text": FromNode(node="base")}),
                OrchestratorGate(id="cleared", charge=CHARGE, reviews=FromNode(node="arm"), rejected_to="arm"),
                JudgeGate(
                    id="judged",
                    work="judge_two",
                    reviews=FromNode(node="base"),
                    inputs={"extra": FromNode(node="cleared")},
                    rejected_to="base",
                ),
            ),
            "output": {"node": "judged"},
        }
    )

    assert validate(spec, registry).ok

    assert await run_workflow(spec, registry, params={}, approval=ApproveAll()) == "b"
    assert decided == ["cleared"]


@pytest.mark.parametrize("rejected_to", ["arm", "cleared"])
def test_an_orchestrator_gates_wait_is_refused_when_the_rewind_misses_an_arm(rejected_to: str) -> None:
    async def base() -> str:
        return "b"

    async def left(text: str) -> str:
        return text + "L"

    async def judge_two(drafts: str, extra: str) -> bool:
        return True

    registry = StepRegistry(decide_gate=None)
    for step in (base, left, judge_two):
        registry.register(step)

    spec = Workflow.model_validate(
        {
            "name": "orchestrator-behind-a-bad-rewind",
            "version": 1,
            "description": "A judge waits on a cleared arm and on the node that arm came from, and rewinds into one.",
            "nodes": (
                RunNode(id="base", work="base"),
                RunNode(id="arm", work="left", inputs={"text": FromNode(node="base")}),
                OrchestratorGate(id="cleared", charge=CHARGE, reviews=FromNode(node="arm"), rejected_to="arm"),
                JudgeGate(
                    id="judged",
                    work="judge_two",
                    reviews=FromNode(node="base"),
                    inputs={"extra": FromNode(node="cleared")},
                    rejected_to=rejected_to,
                ),
            ),
            "output": {"node": "judged"},
        }
    )

    report = validate(spec, registry)

    partial = [issue for issue in report.issues if issue.kind is IssueType.PARTIAL_REWIND]
    assert partial, report.issues
    assert partial[0].where == "judged.rejected_to"
    assert "'judged' would still be waiting on 'base'" in partial[0].detail
