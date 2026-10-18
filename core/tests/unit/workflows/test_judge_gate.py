"""The judge contract: a brought step's verdict, its wired inputs, and the validator guard it needs.

`kind: judge` already ran a bare `bool` step; this is what changed with it. A judge's work may
be a brought step whose output carries the three-valued verdict `approve`, `reject`,
`cannot_judge` -- `cannot_judge` escalates to the same approval hook a person gate answers,
carrying the judge's own feedback ahead of the value. A judge may take wired inputs beside the
one it reviews. And the validator refuses cold a judge holding a tool the host has not declared
safe for a reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import UsageLimitExceeded

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    prepare,
    run_workflow,
    validate,
)
from core.ai.workflows.domain.validator import IssueType
from core.ai.workflows.domain.vocabulary import VERDICTS
from tests.unit.workflows.conftest import ApproveAll

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.registry import StepImplementation

_STRING = Shape(json_schema={"type": "string"})
_VERDICT_SHAPE = Shape(
    json_schema={
        "type": "object",
        "properties": {"outcome": {"enum": list(VERDICTS)}, "feedback": {"type": "string"}},
        "required": ["outcome", "feedback"],
    }
)


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """A `PerformStep` that must never run -- these tests answer through the `StepExecutor` seam."""
    del output_type, inputs
    msg = f"the recording executor answers for {task.name!r}; the briefing must never run"
    raise AssertionError(msg)


async def _draft(topic: str) -> str:
    """The upstream step a judge reviews. Its body never runs in a validator-only test."""
    raise AssertionError(topic)


def _judge_task(*, needs_collection: bool = False, tools: tuple[str, ...] = (), output: Shape = _VERDICT_SHAPE) -> Briefing:
    """A brought judge: reads the drafts (and, when asked, which collection they belong to)."""
    inputs: dict[str, Shape] = {"drafts": _STRING}
    if needs_collection:
        inputs["collection"] = _STRING
    return Briefing(
        name="check",
        instructions="Check the drafts and decide whether they are ready to publish, in full detail and with care.",
        inputs=inputs,
        output=output,
        tools=tools,
    )


def _judged_spec(task: Briefing, **gate_overrides: object) -> Workflow:
    """One drafting step, and a brought judge that reviews what it produced."""
    node: dict[str, object] = {
        "kind": "judge",
        "id": "check",
        "work": task.name,
        "reviews": {"node": "drafted"},
        "approved_when": "outcome",
        "rejected_to": "drafted",
    }
    node.update(gate_overrides)
    return Workflow(
        name="judged-drafts",
        version=1,
        description="Draft from a topic, then let a brought judge decide whether it is ready.",
        parameters={
            "topic": ParameterSpec(description="what to draft", json_schema={"type": "string"}),
            "collection": ParameterSpec(description="which collection this belongs to", json_schema={"type": "string"}),
        },
        inline=(task,),
        nodes=(
            RunNode(id="drafted", work="draft", inputs={"topic": FromParameter(parameter="topic")}),
            JudgeGate.model_validate(node),
        ),
        output=FromNode(node="check"),
    )


def _registry(*, grants: tuple[str, ...] = (), judge_tools: frozenset[str] | None = None) -> StepRegistry:
    reg = StepRegistry(perform=_never_performed, grants=grants, judge_tools=judge_tools)
    reg.register(_draft, name="draft")
    return reg


# --- vocabulary and the verdict check --------------------------------------------------------


def test_a_judge_verdict_may_be_the_three_valued_set() -> None:
    report = validate(_judged_spec(_judge_task()), _registry())

    assert report.ok, report.issues


def test_a_judge_verdict_of_another_set_is_refused() -> None:
    task = _judge_task(
        output=Shape(
            json_schema={
                "type": "object",
                "properties": {"outcome": {"enum": ["yes", "no"]}, "feedback": {"type": "string"}},
                "required": ["outcome", "feedback"],
            }
        )
    )

    report = validate(_judged_spec(task), _registry())

    assert not report.ok
    bad = [issue for issue in report.issues if issue.kind is IssueType.BAD_VERDICT]
    assert bad, report.issues
    assert bad[0].detail.endswith("not bool and not the verdict set approve, reject, cannot_judge")


# --- the read-only guard -----------------------------------------------------------------------


def test_a_judge_step_that_can_act_is_refused() -> None:
    task = _judge_task(tools=("search", "write_note"))
    registry = _registry(grants=("search", "fetch", "write_note"), judge_tools=frozenset({"search", "fetch"}))
    report = validate(_judged_spec(task), registry)

    assert not report.ok
    acting = [issue for issue in report.issues if issue.kind is IssueType.JUDGE_ACTS]
    assert acting, report.issues
    assert acting[0].where == "check"
    assert acting[0].detail == "judge 'check' holds write_note, which can act. A judge only reads, so it may hold: fetch, search"


def test_a_judge_step_is_unchecked_when_the_host_declares_no_read_set() -> None:
    task = _judge_task(tools=("search", "write_note"))
    registry = _registry(grants=("search", "write_note"))
    report = validate(_judged_spec(task), registry)

    assert not any(issue.kind is IssueType.JUDGE_ACTS for issue in report.issues), report.issues


# --- wired inputs --------------------------------------------------------------------------


def test_a_judge_takes_wired_inputs_beside_the_reviewed_value() -> None:
    task = _judge_task(needs_collection=True)

    wired = validate(_judged_spec(task, inputs={"collection": {"parameter": "collection"}}), _registry())
    assert wired.ok, wired.issues

    unwired = validate(_judged_spec(task), _registry())
    assert not unwired.ok
    mismatch = [issue for issue in unwired.issues if issue.kind is IssueType.INPUT_MISMATCH]
    assert mismatch, unwired.issues
    assert any("collection" in issue.detail for issue in mismatch)


# --- the compiler: cannot_judge, reject, a bare bool, wired inputs at run time ---------------


@dataclass
class ScriptedRun:
    """A `StepExecutor` for these compiler tests.

    The drafting step answers a fixed value, and a judge's own node id answers a scripted
    sequence of verdicts, one payload consumed per call. Every call is recorded, kwargs included.
    """

    drafts: str
    verdicts: Mapping[str, list[dict[str, object]]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append((node_id, dict(kwargs)))
        if node_id == "drafted":
            return self.drafts
        return implementation.output_type(**self.verdicts[node_id].pop(0))


async def test_a_cannot_judge_verdict_parks_for_the_persons_with_the_reviewers_note() -> None:
    registry = _registry()
    executor = ScriptedRun(drafts="", verdicts={"check": [{"outcome": "cannot_judge", "feedback": "The drafts are empty."}]})
    approval = ApproveAll()

    result = await run_workflow(
        _judged_spec(_judge_task()), registry, params={"topic": "t", "collection": "repo-docs"}, approval=approval, executor=executor
    )

    assert result == ""
    assert approval.reviewed == [("check", "", "The drafts are empty.")]


@dataclass
class BudgetBlown:
    """A `StepExecutor` whose judge spends its budget before answering, the way a real run refuses past a limit."""

    drafts: str
    calls: list[str] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        del implementation, kwargs
        self.calls.append(node_id)
        if node_id == "drafted":
            return self.drafts
        msg = "The next tool call(s) would exceed the tool_calls_limit of 6 (tool_calls=8)."
        raise UsageLimitExceeded(msg)


async def test_a_judge_that_runs_out_of_budget_cannot_judge_and_parks_with_the_stop_as_its_note() -> None:
    registry = _registry()
    executor = BudgetBlown(drafts="draft text")
    approval = ApproveAll()

    result = await run_workflow(
        _judged_spec(_judge_task()), registry, params={"topic": "t", "collection": "repo-docs"}, approval=approval, executor=executor
    )

    assert result == "draft text", "the person approved, so the value passes through"
    assert approval.reviewed == [
        (
            "check",
            "draft text",
            (
                "The reviewer ran out of budget before it could decide. The next tool call(s) would exceed the "
                "tool_calls_limit of 6 (tool_calls=8)."
            ),
        )
    ]
    assert executor.calls == ["drafted", "check"], "the run neither failed nor reran the drafting node"


async def test_a_reject_verdict_reruns_rejected_to() -> None:
    registry = _registry()
    executor = ScriptedRun(
        drafts="draft text",
        verdicts={"check": [{"outcome": "reject", "feedback": "not ready"}, {"outcome": "approve", "feedback": ""}]},
    )

    result = await run_workflow(
        _judged_spec(_judge_task()), registry, params={"topic": "t", "collection": "repo-docs"}, approval=ApproveAll(), executor=executor
    )

    assert result == "draft text"
    assert sum(1 for node_id, _ in executor.calls if node_id == "drafted") == 2, "the rejection must rewind the drafting node"


async def test_a_bool_judge_still_works() -> None:
    async def draft() -> str:
        return "fine"

    async def judge_plainly(text: str) -> bool:
        return "fine" in text

    registry = StepRegistry()
    registry.register(draft)
    registry.register(judge_plainly)
    spec = Workflow(
        name="bool-judge",
        version=1,
        description="A judge whose step returns a bare bool, exercised end to end.",
        nodes=(
            RunNode(id="drafted", work="draft"),
            JudgeGate(id="check", work="judge_plainly", reviews=FromNode(node="drafted"), rejected_to="drafted"),
        ),
        output=FromNode(node="check"),
    )

    result = await run_workflow(spec, registry, params={}, approval=ApproveAll())

    assert result == "fine"


async def test_wired_inputs_reach_the_judge() -> None:
    task = _judge_task(needs_collection=True)
    registry = _registry()
    spec = _judged_spec(task, inputs={"collection": {"parameter": "collection"}})
    executor = ScriptedRun(drafts="draft text", verdicts={"check": [{"outcome": "approve", "feedback": ""}]})

    await run_workflow(spec, registry, params={"topic": "t", "collection": "repo-docs"}, approval=ApproveAll(), executor=executor)

    _, kwargs = next(call for call in executor.calls if call[0] == "check")
    assert kwargs["collection"] == "repo-docs"
    assert kwargs["drafts"] == "draft text"


def test_prepare_builds_the_brought_judge_before_checking_it() -> None:
    """`prepare`, not just `validate`, is the door these tests exercise upstream of.

    A sanity check that a brought judge builds cleanly through the same seam every other
    brought step does.
    """
    prepared = prepare(_judged_spec(_judge_task()), _registry())

    assert prepared.report.ok, prepared.report.issues
    assert "check" in prepared.registry.names
