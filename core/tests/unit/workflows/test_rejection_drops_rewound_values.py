"""A rejection marks the outputs of the node it returns to and of everything downstream of it as rewound, and keeps their values."""

from __future__ import annotations

import asyncio

import pytest

from pydantic import TypeAdapter

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    MapNode,
    PersonGate,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowState,
    build_graph,
    validate,
)
from core.ai.workflows.exceptions import RetryExhaustedError


class _Snapshots:
    """Records the run's values and rewound keys the moment a chosen node starts for the second time."""

    def __init__(self, state: WorkflowState, watched: str) -> None:
        self.state = state
        self.watched = watched
        self.starts = 0
        self.snapshot: dict[str, object] = {}
        self.rewound: set[str] = set()

    def started(self, node_id: str) -> None:
        if node_id != self.watched:
            return
        self.starts += 1
        if self.starts == 2:
            self.snapshot = dict(self.state.values)
            self.rewound = set(self.state.rewound)

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        del node_id, failed


class _RejectOnce:
    """Approves every gate except `rejecting`, which it rejects on its first review."""

    def __init__(self, rejecting: str) -> None:
        self.rejecting = rejecting
        self.rejected = False

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del value, note
        if gate_id == self.rejecting and not self.rejected:
            self.rejected = True
            return False
        return True


async def test_a_rejection_marks_the_target_and_everything_downstream_of_it_as_rewound() -> None:
    registry = StepRegistry()

    async def seed() -> str:
        return "seed"

    async def draft(seed: str) -> str:
        return f"draft of {seed}"

    async def polish(check: str) -> str:
        return f"polished {check}"

    for step in (seed, draft, polish):
        registry.register(step)
    workflow = Workflow(
        name="rewound",
        version=1,
        description="a rejection from the last gate back to the draft, across an earlier gate",
        nodes=(
            RunNode(id="seed", work="seed"),
            RunNode(id="draft", work="draft", inputs={"seed": FromNode(node="seed")}),
            PersonGate(id="check", reviews=FromNode(node="draft"), rejected_to="draft"),
            RunNode(id="polish", work="polish", inputs={"check": FromNode(node="check")}),
            PersonGate(id="final", reviews=FromNode(node="polish"), rejected_to="draft"),
        ),
        output=FromNode(node="final"),
    )
    assert validate(workflow, registry).ok, validate(workflow, registry).issues

    state = WorkflowState(params={})
    observer = _Snapshots(state, "draft")
    graph = build_graph(workflow, registry, approval=_RejectOnce("final"), observer=observer)
    await graph.run(state=state)

    assert observer.starts == 2
    assert observer.rewound == {"draft", "check", "polish", "final"}, "the target and everything downstream of it"
    assert "seed" not in observer.rewound
    assert {"seed", "draft", "check", "polish"} <= observer.snapshot.keys(), "the values themselves are kept"
    assert state.rewound == set(), "the rerun wrote every rewound key again"


async def test_a_rejection_to_a_multi_turn_map_marks_its_turn_keys_as_rewound() -> None:
    registry = StepRegistry()

    async def items() -> list[str]:
        return ["one"]

    registry.register(items)

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task
        return TypeAdapter(output_type).validate_python(next(iter(inputs.values())))

    registry.perform = perform
    text = Shape(json_schema={"type": "string"})
    flag = Briefing(name="flag", instructions="Flag the given item, exactly as briefed here.", inputs={"item": text}, output=text)
    draft = Briefing(name="draft", instructions="Draft from the flagged item, exactly as briefed here.", inputs={"flag": text}, output=text)
    workflow = Workflow(
        name="rewound-map",
        version=1,
        description="a person gate rejecting back to a two-turn map",
        inline=(flag, draft),
        nodes=(
            RunNode(id="items", work="items"),
            MapNode(id="drafted", turns=("flag", "draft"), over=FromNode(node="items"), each="item"),
            PersonGate(id="review", reviews=FromNode(node="drafted"), rejected_to="drafted"),
        ),
        output=FromNode(node="review"),
    )
    assert validate(workflow, registry).ok, validate(workflow, registry).issues

    state = WorkflowState(params={})
    observer = _Snapshots(state, "drafted.flag")
    graph = build_graph(workflow, registry, approval=_RejectOnce("review"), observer=observer)
    await graph.run(state=state)

    assert observer.starts == 2
    assert observer.rewound == {"drafted", "drafted.flag", "drafted.draft", "review"}
    assert {"items", "drafted", "drafted.flag", "drafted.draft"} <= observer.snapshot.keys(), "the values themselves are kept"
    assert state.rewound == set()


async def test_a_run_whose_attempts_are_spent_keeps_the_last_value_of_the_target() -> None:
    registry = StepRegistry()

    async def draft() -> str:
        return "last draft"

    registry.register(draft)
    workflow = Workflow(
        name="exhausted",
        version=1,
        description="a gate that rejects until its attempts are spent",
        nodes=(
            RunNode(id="draft", work="draft"),
            PersonGate(id="check", reviews=FromNode(node="draft"), rejected_to="draft", max_attempts=2),
        ),
        output=FromNode(node="check"),
    )

    class _RejectAll:
        async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
            del gate_id, value, note
            return False

    state = WorkflowState(params={})
    with pytest.raises(RetryExhaustedError):
        await build_graph(workflow, registry, approval=_RejectAll()).run(state=state)

    assert state.values["draft"] == "last draft"


@pytest.mark.parametrize("settle", range(6))
@pytest.mark.parametrize("hold", range(6))
async def test_a_branch_still_running_below_the_target_finishes_after_the_rejection(hold: int, settle: int) -> None:
    """Branch `a` waits for the rejection, then `hold` scheduler turns, while the gate releases it `settle` turns after deciding to reject."""
    registry = StepRegistry()
    rejected = asyncio.Event()

    async def draft() -> str:
        return "draft"

    async def step_a(draft: str) -> str:
        await rejected.wait()
        for _ in range(hold):
            await asyncio.sleep(0)
        return f"a of {draft}"

    async def step_b(a: str) -> str:
        return f"b of {a}"

    async def combine(check: str, b: str) -> str:
        return f"{check} | {b}"

    for step in (draft, step_a, step_b, combine):
        registry.register(step)
    workflow = Workflow(
        name="sibling",
        version=1,
        description="a gate rejects while a sibling branch below its target is still running",
        nodes=(
            RunNode(id="draft", work="draft"),
            PersonGate(id="check", reviews=FromNode(node="draft"), rejected_to="draft"),
            RunNode(id="a", work="step_a", inputs={"draft": FromNode(node="draft")}),
            RunNode(id="b", work="step_b", inputs={"a": FromNode(node="a")}),
            RunNode(id="final", work="combine", inputs={"check": FromNode(node="check"), "b": FromNode(node="b")}),
        ),
        output=FromNode(node="final"),
    )
    assert validate(workflow, registry).ok, validate(workflow, registry).issues

    class _ReleaseOnReject(_RejectOnce):
        async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
            verdict = await super().review(gate_id, value, note=note)
            if not verdict:
                for _ in range(settle):
                    await asyncio.sleep(0)
                rejected.set()
            return verdict

    state = WorkflowState(params={})
    result = await build_graph(workflow, registry, approval=_ReleaseOnReject("check")).run(state=state)

    assert result == "draft | b of a of draft"
