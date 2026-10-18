"""The shipped flag workflow, run end to end off its own files, with groups whose worker gives up.

The graph and the steps come from the real documents under `templates/research/workflows` and
`harness/workflows/steps`, never from a copy typed out here, so the failure policy the template declares
is the one tested. Every step is scripted and no model is reached. The failure each case scripts is the
one a thinking model raises when it spends the whole response ceiling before answering.

What the cases cover: one group that fails is left out and the run carries on to the end with the other
groups' flags, and three groups failing the same way in a row end the run as failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.workflows.contracts import (
    GateDecision,
    StepRegistry,
    Workflow,
    WorkflowState,
    build_graph,
    load_spec_document,
    load_step_document,
    step_page_id,
)
from pydantic import BaseModel, TypeAdapter
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS
from tests.support.grants import app_grants, app_reads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from core.ai.workflows.contracts import Briefing, InlineStep, WorkflowArguments
    from core.ai.workflows.registry import StepImplementation

HARNESS_STEPS = Path(__file__).resolve().parents[2] / "workflows" / "steps"
RESEARCH = Path(__file__).resolve().parents[3] / "templates" / "research" / "workflows"

PARAMS: WorkflowArguments = {"collection": "research", "digest_ids": ""}

TOKEN_LIMIT = (
    "Model token limit (8192) exceeded before any response was generated. Increase the `max_tokens` model setting, "
    "or simplify the prompt to result in a shorter response that will fit within the limit."
)
"""The words the model route raised in the flag run that failed, verbatim."""


def _steps_by_id() -> dict[str, InlineStep]:
    """Every harness and research step file, under the id the library mints for it."""
    paths = [*sorted(HARNESS_STEPS.glob("*.md")), *sorted((RESEARCH / "steps").glob("*.md"))]
    documents = [load_step_document(path.read_text(encoding="utf-8")) for path in paths]
    return {step_page_id(doc.step.name, doc.version): doc.step for doc in documents}


def _spec() -> Workflow:
    """The shipped flag workflow with the steps its `uses` names copied in, exactly as an import copies them."""
    text = (RESEARCH / "workflows" / "research-compound.md").read_text(encoding="utf-8")
    spec = load_spec_document(text).workflow
    by_id = _steps_by_id()
    uses = list(yaml.safe_load(text.split("---", 2)[1]).get("uses", []))
    missing = [step_id for step_id in uses if step_id not in by_id]
    assert not missing, f"the workflow names {missing}, which no shipped step file declares"
    return spec.model_copy(update={"inline": (*spec.inline, *(by_id[step_id] for step_id in uses))})


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """A performer that must never run: every step here is answered through the scripted executor."""
    del output_type, inputs
    msg = f"the scripted executor answers for {task.name!r}"
    raise AssertionError(msg)


async def _no_tool(name: str, args: dict[str, object], /) -> object:
    """A tool caller that must never run: the flag workflow's programs call no tool."""
    msg = f"no program in this workflow calls a tool, yet {name!r} was called"
    raise AssertionError(msg)


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


async def _approve(gate_id: str, charge: str, value: object, /) -> GateDecision:
    """The orchestrator, approving whatever the merge produced."""
    del gate_id, charge, value
    return GateDecision(approve=True)


async def _registry(sandbox: AsyncMonty) -> StepRegistry:
    """A registry offering exactly what this app offers a declared step, plus the host's own `notify_owner`."""
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_never_performed,
        grants=grantable,
        run_code=code_runner(sandbox, reads=await app_reads(), call=_no_tool),
        decide_gate=_approve,
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )

    async def notify_owner(collection: str, written: int, needs_you: int = 0) -> str:
        return f"{written} in {collection}"

    registry.register(notify_owner, name="notify_owner", replay_safe=False)
    return registry


class NoPerson:
    """The person a gate would escalate to. This workflow's one gate is an orchestrator's, so nobody is asked."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del value, note
        msg = f"nobody should be asked about {gate_id!r}"
        raise AssertionError(msg)


class _Group(BaseModel):
    group_id: int


def _typed(implementation: StepImplementation, payload: object) -> object:
    """`payload` as the step's own declared output type, so a scripted answer is typed like a real one."""
    return TypeAdapter(implementation.output_type).validate_python(payload)


@dataclass
class Scripted:
    """Every step of the flag workflow, scripted.

    `groups` is how many groups `list_digests` hands the map, and `failing` names the groups whose worker
    raises the token-limit error. `calls` records every node performed, in order, and `candidate_lists`
    records how many groups' candidates each merge received.
    """

    groups: int
    failing: frozenset[int]
    calls: list[str] = field(default_factory=list)
    candidate_lists: list[int] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append(node_id)
        if node_id == "list_digests":
            groups = [
                {
                    "group_id": n,
                    "members": [
                        {
                            "digest_id": f"paper.digest-{n}",
                            "page_type": "paper",
                            "title": f"Digest {n}",
                            "source_id": f"src-{n}",
                            "path": f"Wiki/Papers/digest-{n}.md",
                            "findings": [],
                        }
                    ],
                }
                for n in range(self.groups)
            ]
            return _typed(implementation, groups)
        if node_id == "propose_flags":
            group_id = _Group.model_validate(to_jsonable_python(kwargs["group"])).group_id
            if group_id in self.failing:
                raise UnexpectedModelBehavior(TOKEN_LIMIT)
            flag = {
                "title": f"A flag from group {group_id}",
                "summary": "One sentence the owner reads first.",
                "flag_kind": "idea",
                "about": [f"paper.digest-{group_id}"],
                "why": "It bears on the base's purpose.",
                "proposal": "Try it on one case.",
            }
            return _typed(implementation, {"flags": [flag], "defects": []})
        if node_id == "merge_candidates":
            candidates = kwargs["candidates"]
            assert isinstance(candidates, list)
            self.candidate_lists.append(len(candidates))
            merged = to_jsonable_python(candidates)
            return _typed(
                implementation,
                {"flags": [flag for group in merged for flag in group["flags"]], "defects": [defect for group in merged for defect in group["defects"]]},
            )
        if node_id == "write_flags":
            approved = to_jsonable_python(kwargs["approved"])
            return len(approved["flags"])
        if node_id == "finish":
            return _typed(implementation, {"notified": kwargs["notified"]})
        return await implementation.function(**kwargs)


async def test_one_group_whose_model_gives_up_is_left_out_and_the_run_reaches_the_end(sandbox: AsyncMonty) -> None:
    """Group 2 fails, the other three groups' flags are merged, written and notified."""
    scripted = Scripted(groups=4, failing=frozenset({2}))
    state = WorkflowState(params=PARAMS)

    result = await build_graph(_spec(), await _registry(sandbox), approval=NoPerson(), executor=scripted).run(state=state)

    assert to_jsonable_python(result) == {"notified": "3 in research"}
    assert state.skipped == {"propose_flags": [TOKEN_LIMIT]}
    assert scripted.candidate_lists == [3], "the merge reads the three groups that answered"
    after_merge = scripted.calls[scripted.calls.index("merge_candidates") + 1 :]
    assert set(after_merge[:-2]) == {"write_flags", "list_repairs", "count_owner_repairs"}
    assert after_merge[-2:] == ["notify", "finish"]


async def test_three_groups_failing_the_same_way_in_a_row_end_the_run(sandbox: AsyncMonty) -> None:
    """Every group raises the token-limit error, so the third one aborts the run before anything is merged."""
    scripted = Scripted(groups=3, failing=frozenset({0, 1, 2}))
    state = WorkflowState(params=PARAMS)

    with pytest.raises(RuntimeError, match=r"map 'propose_flags' aborted: 3 consecutive items failed with UnexpectedModelBehavior"):
        await build_graph(_spec(), await _registry(sandbox), approval=NoPerson(), executor=scripted).run(state=state)

    assert state.skipped == {"propose_flags": [TOKEN_LIMIT, TOKEN_LIMIT]}
    assert "merge_candidates" not in scripted.calls
