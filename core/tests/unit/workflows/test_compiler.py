"""End to end: the quarterly report compiles and runs; rejection rewinds; invalid specs never compile.

The second half is the identity of one piece of work: which item of a fan-out a disposition belongs
to, which generation of that fan-out's input it was minted under, and what a rewind does to both.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    PersonGate,
    RunNode,
    StepRegistry,
    Workflow,
    WorkflowArguments,
    WorkflowResultSpec,
    WorkflowState,
    WorkItemFact,
    build_graph,
    observed_outcome,
    run_workflow,
)
from core.ai.workflows.exceptions import WorkflowInvalidError
from tests.unit.workflows.conftest import ApproveAll, CallLog, RejectFirst, ReportUrl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.internal_contracts import ApprovalHook


async def test_happy_path_publishes(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    approval = ApproveAll()
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=approval)
    assert isinstance(result, ReportUrl)
    # the two fetches run concurrently (either order); everything downstream is ordered
    assert set(call_log.calls[:2]) == {"fetch_sales", "fetch_costs"}
    assert call_log.calls[2:] == ["reconcile", "draft_report", "publish"]
    assert len(approval.reviewed) == 1
    assert approval.reviewed[0][0] == "approve"


async def test_rejection_rewinds_to_draft(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    approval = RejectFirst()
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=approval)
    assert isinstance(result, ReportUrl)
    assert approval.reviews == 2
    assert call_log.calls.count("draft_report") == 2, "rejection must re-run the draft"
    assert call_log.calls.count("publish") == 1
    assert call_log.calls.count("fetch_sales") == 1, "the rewind must not repay completed upstream work"


async def test_parameter_wire_is_runtime_validated(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A parameter that does not parse as the step's type fails at the wire, not inside the step."""
    with pytest.raises(ValidationError) as exc_info:
        await run_workflow(quarterly_report, registry, params={"quarter": {"not": "a quarter"}}, approval=ApproveAll())
    assert "str" in str(exc_info.value)


def test_invalid_spec_never_compiles(registry: StepRegistry) -> None:
    bad = Workflow(
        name="broken",
        version=1,
        description="publish wired past its inputs",
        nodes=(RunNode(id="publish", work="publish", inputs={"report": FromNode(node="ghost")}),),
        output=FromNode(node="publish"),
    )
    with pytest.raises(WorkflowInvalidError) as exc_info:
        build_graph(bad, registry, approval=ApproveAll())
    assert "ghost" in str(exc_info.value)


def test_compile_is_deterministic(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Same spec, same registry -- same graph shape, twice. Determinism is what replay buys."""
    g1 = build_graph(quarterly_report, registry, approval=ApproveAll())
    g2 = build_graph(quarterly_report, registry, approval=ApproveAll())
    assert g1.render() == g2.render()


async def test_unapproved_publish_is_unreachable(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    """The envelope claim, executed: while the gate keeps rejecting, publish cannot run."""

    class NeverApprove:
        def __init__(self) -> None:
            self.reviews = 0

        async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
            self.reviews += 1
            if self.reviews >= 3:
                msg = "operator gave up"
                raise RuntimeError(msg)
            return False

    with pytest.raises(RuntimeError, match="operator gave up"):
        await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=NeverApprove())
    assert "publish" not in call_log.calls
    assert call_log.calls.count("draft_report") == 3, "every rejection rewound to the draft"


# --- what one item of work is, and what settles it ---------------------------------------------


@dataclass
class Batches:
    """The lists `seed` hands the map, one per reach, so a rewind can change the input."""

    lists: list[list[str]]
    reach: int = 0

    def next(self) -> list[str]:
        batch = self.lists[min(self.reach, len(self.lists) - 1)]
        self.reach += 1
        return list(batch)


def _fan_out_spec(*, on_error: Literal["fail", "skip"] = "fail", max_parallel: int | None = None) -> Workflow:
    """Seed a list, shout every item of it, and let one gate rewind the seed."""
    return Workflow(
        name="fan-out",
        version=1,
        description="Seed a list, shout every item, and review the shouting.",
        parameters={"tag": ParameterSpec(description="anything", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="seed", work="seed", inputs={"tag": FromParameter(parameter="tag")}),
            MapNode(id="each", turns=("shout",), over=FromNode(node="seed"), each="word", on_error=on_error, max_parallel=max_parallel),
            PersonGate(id="approve", reviews=FromNode(node="each"), rejected_to="seed"),
        ),
        output=FromNode(node="approve"),
    )


def _fan_out_registry(
    batches: Batches,
    *,
    explodes: str = "",
    waits: _Held | None = None,
    delays: Mapping[tuple[int, str], float] | None = None,
    finished: list[tuple[int, str]] | None = None,
) -> StepRegistry:
    """`delays` holds one item back by `(reach, word)`, so a test can choose what finishes when."""
    reg = StepRegistry()

    async def seed(tag: str) -> list[str]:
        del tag
        return batches.next()

    async def shout(word: str) -> str:
        if word == explodes:
            msg = f"{word} cannot be shouted"
            raise RuntimeError(msg)
        if waits is not None and word == "wait":
            waits.entered.set()
            await waits.release.wait()
        if delays is not None:
            await asyncio.sleep(delays.get((batches.reach, word), 0.0))
        if finished is not None:
            finished.append((batches.reach, word))
        return word.upper()

    reg.register(seed)
    reg.register(shout)
    return reg


@dataclass
class _Held:
    """One item held mid-flight: it says when it started, and waits to be let go."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


def _facts_of(state: WorkflowState, node_id: str) -> list[WorkItemFact]:
    """One node's settled dispositions, oldest identity first."""
    return [fact for fact in state.work_facts.values() if fact.node_id == node_id]


async def _run_to_state(spec: Workflow, registry: StepRegistry, approval: ApprovalHook, params: WorkflowArguments) -> WorkflowState:
    state = WorkflowState(params=params)
    graph = build_graph(spec, registry, approval=approval)
    await graph.run(state=state)
    return state


async def test_every_item_of_a_fan_out_settles_exactly_one_disposition() -> None:
    batches = Batches(lists=[["a", "b", "boom"]])
    state = await _run_to_state(
        _fan_out_spec(on_error="skip"), _fan_out_registry(batches, explodes="boom"), ApproveAll(), {"tag": "t"}
    )

    facts = _facts_of(state, "each")
    assert len(facts) == 3, "one final disposition per item, never one per turn or per finish"
    assert [fact.disposition for fact in facts] == ["succeeded", "succeeded", "skipped"]
    assert {fact.generation for fact in facts} == {0}
    assert state.batches["each"].items == tuple(fact.item_key for fact in facts)


async def test_a_run_node_settles_under_generation_zero_and_the_empty_item_key() -> None:
    batches = Batches(lists=[["a"]])
    state = await _run_to_state(_fan_out_spec(), _fan_out_registry(batches), ApproveAll(), {"tag": "t"})

    (seeded,) = _facts_of(state, "seed")
    assert (seeded.generation, seeded.item_key, seeded.disposition) == (0, "", "succeeded")
    assert state.batches["seed"].items == ("",)


async def test_a_rewind_over_the_same_items_reuses_every_key_and_counts_the_attempt() -> None:
    """The second reach finishes its items in the opposite order, and the keys still match item for item.

    An identity minted from whichever item happened to start first would drift the moment the
    scheduler answered differently. These come from source order, so a rewind is the same work.
    """
    batches = Batches(lists=[["a", "b"], ["a", "b"]])
    finished: list[tuple[int, str]] = []
    # Reach one lets `a` land first and reach two holds it back, so the two passes settle in
    # opposite orders while the collection they map over is byte for byte the same.
    delays = {(1, "b"): 0.05, (2, "a"): 0.05}
    registry = _fan_out_registry(batches, delays=delays, finished=finished)
    state = await _run_to_state(_fan_out_spec(), registry, RejectFirst(), {"tag": "t"})

    assert finished == [(1, "a"), (1, "b"), (2, "b"), (2, "a")], "the second reach really did settle in reverse"
    assert state.batches["each"].generation == 0, "the same ordered input stayed the same generation"
    keys = state.batches["each"].items
    facts = _facts_of(state, "each")
    assert len(facts) == 2, "the rerun updated the identities it already had"
    assert [fact.item_key for fact in facts] == list(keys), "every key came back, in source order"
    assert [key.split(":")[1] for key in keys] == ["0", "1"], "and each still names the index of its own item"
    assert [fact.retry_attempts for fact in facts] == [1, 1]
    assert [fact.disposition for fact in facts] == ["succeeded", "succeeded"]
    assert _facts_of(state, "seed")[0].retry_attempts == 1, "the rewound run node counts its attempt too"


@pytest.mark.parametrize(
    ("second", "why"),
    [
        (["a"], "one item where there were two"),
        ([], "no items at all"),
        (["b", "a"], "the same two items in the other order"),
        (["a", "b", "c"], "one item added"),
        (["a", "c"], "one item swapped out"),
    ],
)
async def test_any_change_to_the_mapped_input_opens_a_new_generation(second: list[str], why: str) -> None:
    batches = Batches(lists=[["a", "b"], second])
    state = await _run_to_state(_fan_out_spec(), _fan_out_registry(batches), RejectFirst(), {"tag": "t"})

    batch = state.batches["each"]
    assert batch.generation == 1, why
    assert batch.items == tuple(fact.item_key for fact in _facts_of(state, "each") if fact.generation == 1)
    assert len(batch.items) == len(second)
    audit = [fact for fact in _facts_of(state, "each") if fact.generation == 0]
    assert len(audit) == 2, "the generation that was superseded is kept as audit"
    assert all(fact.retry_attempts == 0 for fact in _facts_of(state, "each")), "a new identity is not a retry"


async def test_two_equal_items_are_two_items_because_their_index_is_part_of_the_key() -> None:
    batches = Batches(lists=[["a", "a"]])
    state = await _run_to_state(_fan_out_spec(), _fan_out_registry(batches), ApproveAll(), {"tag": "t"})

    keys = state.batches["each"].items
    assert len(set(keys)) == 2, "equal values at different indices are distinct work"
    assert [key.split(":")[1] for key in keys] == ["0", "1"]
    assert len({key.split(":")[2] for key in keys}) == 1, "and they still address the same value"


async def test_a_fan_out_interrupted_before_one_key_settles_leaves_that_key_unsettled() -> None:
    batches = Batches(lists=[["a", "wait"]])
    held = _Held()
    # One slot, so `a` settles before `wait` even starts: the interruption lands on a known point.
    spec, registry = _fan_out_spec(max_parallel=1), _fan_out_registry(batches, waits=held)
    state = WorkflowState(params={"tag": "t"})
    graph = build_graph(spec, registry, approval=ApproveAll())

    running = asyncio.create_task(graph.run(state=state))
    await held.entered.wait()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    settled = {fact.item_key for fact in _facts_of(state, "each")}
    assert len(settled) == 1
    assert set(state.batches["each"].items) - settled, "the active key set still owes a disposition"
    assert observed_outcome(
        WorkflowResultSpec(summary="the shouting", primary_work="each"),
        status="failed",
        facts=_facts_of(state, "each"),
        primary=state.batches["each"],
        decisions=state.gate_decisions,
        reviews={},
    ).status == "unknown"


async def test_a_declared_empty_primary_is_a_no_op_and_an_undeclared_one_is_unknown() -> None:
    batches = Batches(lists=[[]])
    state = await _run_to_state(_fan_out_spec(), _fan_out_registry(batches), ApproveAll(), {"tag": "t"})

    assert state.batches["each"].items == ()
    assert not _facts_of(state, "each")
    declared = observed_outcome(
        WorkflowResultSpec(summary="the shouting", primary_work="each", empty_primary="noop"),
        status="completed",
        facts=[],
        primary=state.batches["each"],
        decisions=[],
        reviews={},
    )
    undeclared = observed_outcome(
        WorkflowResultSpec(summary="the shouting", primary_work="each"),
        status="completed",
        facts=[],
        primary=state.batches["each"],
        decisions=[],
        reviews={},
    )
    assert (declared.status, undeclared.status) == ("noop", "unknown")


async def test_the_step_reads_the_item_itself_and_never_the_wrapper_that_carries_its_key() -> None:
    batches = Batches(lists=[["a", "b"]])
    seen: list[object] = []
    reg = StepRegistry()

    async def seed(tag: str) -> list[str]:
        del tag
        return batches.next()

    async def shout(word: str) -> str:
        seen.append(word)
        return word.upper()

    reg.register(seed)
    reg.register(shout)
    state = await _run_to_state(_fan_out_spec(), reg, ApproveAll(), {"tag": "t"})

    assert seen == ["a", "b"]
    assert state.values["each"] == ["A", "B"]
