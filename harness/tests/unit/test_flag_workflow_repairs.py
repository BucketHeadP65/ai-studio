"""The shipped flag workflow v12, run end to end off its own files: defects go to the repair map, flags to write_flags."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from core.ai.workflows.contracts import StepRegistry, WorkflowState, build_graph
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS
from tests.support.grants import app_grants, app_reads
from tests.unit.test_flag_workflow_failures import PARAMS, NoPerson, _approve, _never_performed, _spec, _typed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from core.ai.workflows.registry import StepImplementation


def _member(page_id: str, source_id: str, findings: list[str] | None = None) -> dict[str, object]:
    return {"digest_id": page_id, "page_type": "paper", "title": page_id, "source_id": source_id, "path": f"Wiki/Papers/{page_id}.md", "findings": findings or []}


_FLAG = {"title": "A flag", "summary": "One sentence.", "flag_kind": "idea", "about": ["paper.a"], "why": "It bears on the purpose.", "proposal": "Try it."}


@dataclass
class Repairing:
    """Every briefing of the flag workflow, scripted: the members, the defects the workers name, and how each repair ends."""

    members: list[dict[str, object]]
    defects: list[dict[str, object]] = field(default_factory=list)
    flags: list[dict[str, object]] = field(default_factory=lambda: [dict(_FLAG)])
    outcomes: Mapping[str, str] = field(default_factory=dict)
    raising: frozenset[str] = frozenset()
    repaired_items: list[dict[str, object]] = field(default_factory=list)
    written_flags: list[int] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append(node_id)
        if node_id == "list_digests":
            return _typed(implementation, [{"group_id": 0, "members": self.members}])
        if node_id == "propose_flags":
            return _typed(implementation, {"flags": self.flags, "defects": self.defects})
        if node_id == "merge_candidates":
            candidates = to_jsonable_python(kwargs["candidates"])
            return _typed(implementation, {"flags": [f for c in candidates for f in c["flags"]], "defects": [d for c in candidates for d in c["defects"]]})
        if node_id == "write_flags":
            approved = to_jsonable_python(kwargs["approved"])
            self.written_flags.append(len(approved["flags"]))
            return len(approved["flags"])
        if node_id == "repair_sources":
            item = to_jsonable_python(kwargs["item"])
            self.repaired_items.append(item)
            if item["source_id"] in self.raising:
                msg = "the repair worker gave up"
                raise UnexpectedModelBehavior(msg)
            outcome = self.outcomes.get(item["source_id"], "repaired")
            return _typed(implementation, {"source_id": item["source_id"], "page_ids": item["page_ids"], "defect": item["defect"], "outcome": outcome, "written": item["page_ids"], "detail": "done"})
        if node_id == "finish":
            return _typed(implementation, {"notified": kwargs["notified"]})
        return await implementation.function(**kwargs)


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


async def _no_tool(name: str, args: dict[str, object], /) -> object:
    msg = f"no program in this workflow calls a tool, yet {name!r} was called"
    raise AssertionError(msg)


async def _registry(sandbox: AsyncMonty) -> StepRegistry:
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_never_performed,
        grants=grantable,
        run_code=code_runner(sandbox, reads=await app_reads(), call=_no_tool),
        decide_gate=_approve,
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )

    async def notify_owner(collection: str, written: int, needs_you: int = 0) -> str:
        return f"{written} in {collection}, {needs_you} need you"

    registry.register(notify_owner, name="notify_owner", replay_safe=False)
    return registry


async def _run(sandbox: AsyncMonty, script: Repairing, state: WorkflowState | None = None) -> object:
    graph = build_graph(_spec(), await _registry(sandbox), approval=NoPerson(), executor=script)
    return to_jsonable_python(await graph.run(state=state or WorkflowState(params=PARAMS)))


async def test_a_defect_goes_to_the_repair_map_and_a_flag_to_write_flags(sandbox: AsyncMonty) -> None:
    script = Repairing(members=[_member("paper.a", "src-a")], defects=[{"page_ids": ["paper.a"], "defect": "off_purpose", "evidence": "Physics, not retrieval."}])

    result = await _run(sandbox, script)

    assert result == {"notified": "1 in research, 0 need you"}
    assert script.written_flags == [1]
    assert script.repaired_items == [
        {"source_id": "src-a", "page_ids": ["paper.a"], "pages": [{"page_id": "paper.a", "path": "Wiki/Papers/paper.a.md"}], "defect": "off_purpose", "evidence": "Physics, not retrieval."}
    ]


async def test_a_member_with_a_quote_finding_is_repaired_with_no_defect_from_the_model(sandbox: AsyncMonty) -> None:
    script = Repairing(members=[_member("paper.b.limits", "src-b", ["quote-not-in-source"])])

    await _run(sandbox, script)

    assert [item["defect"] for item in script.repaired_items] == ["misquoted"]
    assert script.repaired_items[0]["page_ids"] == ["paper.b.limits"]


async def test_a_finding_of_another_rule_makes_no_repair_of_its_own(sandbox: AsyncMonty) -> None:
    script = Repairing(members=[_member("paper.c", "src-c", ["oversized-summary"])])
    await _run(sandbox, script)
    assert script.repaired_items == []


async def test_pages_of_one_source_become_one_item_with_the_graver_defect(sandbox: AsyncMonty) -> None:
    members = [_member("paper.d", "src-d"), _member("paper.d.methods", "src-d", ["quote-not-in-source", "missing-loc"])]
    script = Repairing(members=members, defects=[{"page_ids": ["paper.d"], "defect": "off_purpose", "evidence": "Off purpose."}])

    await _run(sandbox, script)

    assert len(script.repaired_items) == 1
    item = script.repaired_items[0]
    assert item["defect"] == "off_purpose"
    assert item["page_ids"] == ["paper.d", "paper.d.methods"]
    assert [page["page_id"] for page in cast("list[dict[str, str]]", item["pages"])] == ["paper.d", "paper.d.methods"]
    assert "missing-loc" in str(item["evidence"]), "another rule rides as evidence on an item that exists"


async def test_repairs_that_fail_or_need_the_owner_are_counted_for_the_notification(sandbox: AsyncMonty) -> None:
    members = [_member("paper.e", "src-e", ["quote-not-in-source"]), _member("paper.f", "src-f", ["quote-not-in-source"])]
    script = Repairing(members=members, flags=[], outcomes={"src-e": "failed", "src-f": "needs_owner"})

    assert await _run(sandbox, script) == {"notified": "0 in research, 2 need you"}


async def test_a_repair_worker_that_raises_is_left_out_and_still_counted(sandbox: AsyncMonty) -> None:
    members = [_member("paper.g", "src-g", ["quote-not-in-source"]), _member("paper.h", "src-h", ["quote-not-in-source"])]
    script = Repairing(members=members, flags=[], raising=frozenset({"src-g"}))
    state = WorkflowState(params=PARAMS)

    assert await _run(sandbox, script, state) == {"notified": "0 in research, 1 need you"}
    assert list(state.skipped) == ["repair_sources"]


async def test_a_pass_with_nothing_to_repair_reaches_the_end(sandbox: AsyncMonty) -> None:
    script = Repairing(members=[_member("paper.i", "src-i")], flags=[])

    assert await _run(sandbox, script) == {"notified": "0 in research, 0 need you"}
    assert script.calls[-2:] == ["notify", "finish"]


async def test_a_defect_naming_a_page_outside_the_batch_becomes_an_item_with_no_pages(sandbox: AsyncMonty) -> None:
    script = Repairing(members=[_member("paper.j", "src-j")], defects=[{"page_ids": ["paper.elsewhere"], "defect": "duplicate", "evidence": "Repeats another."}])

    await _run(sandbox, script)

    assert script.repaired_items == [{"source_id": "", "page_ids": ["paper.elsewhere"], "pages": [], "defect": "duplicate", "evidence": "Repeats another."}]
