"""The shipped stance-refresh workflow, run end to end off its own files.

The graph and the steps come from the real documents under `templates/research/workflows`, never from a copy typed
out here. The three model steps are scripted and no model is reached. The four programs run for real in the
sandbox, and every tool they call answers from a scripted records base and a scripted research base, so nothing
reaches a real knowledge base.

What the cases cover: a stance whose record changed is rewritten and written to its own path, a rewrite that alters
Evidence is refused at landing, a refused write is reported beside the written one, more than twenty-five changed
pages keep none, a run that admits nothing makes one judge call on an empty list and ends noop, two groups proposing
the same new stance make one item, a changed page the records base does not list leaves its stance alone, a changed
page whose file lies outside the records base's root is still admitted, a repeated pair is listed once, and a full
pass lists every pair with no cap. Then the stances the run cannot read: one read_page refuses and one carrying a
field the workflow does not declare are both named and left alone while the run carries on, and a new draft never
takes the id of a stance that exists or a path the model chose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest
import yaml

from core.ai.workflows.contracts import (
    StepRegistry,
    Workflow,
    WorkflowState,
    build_graph,
    load_spec_document,
    load_step_document,
    step_page_id,
)
from pydantic_ai.exceptions import ModelRetry
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS
from harness.features.running.domain.ended_run import EndedRun, checked_outcome
from tests.support.grants import app_grants, app_reads
from tests.unit.test_flag_workflow_failures import NoPerson, _never_performed, _typed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from core.ai.workflows.contracts import InlineStep
    from core.ai.workflows.registry import StepImplementation

RESEARCH = Path(__file__).resolve().parents[3] / "templates" / "research" / "workflows"

PROGRESS = "adr:accepted, adr:superseded, architecture:current, spec-doc:approved, spec-doc:executed"
"""The pairs the schedule passes, the same for every case here."""

SECTIONS = ("TL;DR", "Position", "Why", "Evidence", "Alternatives", "Open Questions")
"""The stance page type's canonical sections, headed the way read_page heads them."""


class Page(TypedDict):
    page_id: str
    page_type: str
    status: str
    title: str


class Group(TypedDict):
    group_id: int
    pages: list[Page]


class Section(TypedDict):
    heading: str
    level: int
    body: str


class Payload(TypedDict):
    frontmatter: dict[str, object]
    sections: list[Section]


class Item(TypedDict):
    stance_id: str
    title: str
    path: str
    new: bool
    pages: list[Page]
    current: Payload | None


class StanceAsRead(TypedDict):
    stance_id: str
    path: str
    payload: Payload


class WriteCall(TypedDict):
    knowledge_base: str
    payload: Payload
    path: str


def _spec() -> Workflow:
    """The shipped workflow with the steps its `uses` names copied in, exactly as an import copies them."""
    text = (RESEARCH / "workflows" / "research-stance-refresh.md").read_text(encoding="utf-8")
    spec = load_spec_document(text).workflow
    documents = [load_step_document(path.read_text(encoding="utf-8")) for path in sorted((RESEARCH / "steps").glob("*.md"))]
    by_id: dict[str, InlineStep] = {step_page_id(doc.step.name, doc.version): doc.step for doc in documents}
    uses = list(yaml.safe_load(text.split("---", 2)[1]).get("uses", []))
    missing = [step_id for step_id in uses if step_id not in by_id]
    assert not missing, f"the workflow names {missing}, which no shipped step file declares"
    return spec.model_copy(update={"inline": (*spec.inline, *(by_id[step_id] for step_id in uses))})


def _record(page_id: str, status: str = "accepted", page_type: str = "adr") -> dict[str, str]:
    """One records page's frontmatter."""
    return {"id": page_id, "type": page_type, "title": f"Title of {page_id}", "status": status}


def _listed(page_id: str, status: str = "accepted", page_type: str = "adr") -> Page:
    """One records page the way the workflow lists it once admitted."""
    return {"page_id": page_id, "page_type": page_type, "status": status, "title": f"Title of {page_id}"}


def _stance(slug: str, record: str, **fields: object) -> dict[str, object]:
    """One stance page the way read_page answers it, carrying `fields` in its frontmatter beside its own."""
    frontmatter: dict[str, object] = {
        "id": f"stance.{slug}",
        "type": "stance",
        "title": f"Stance on {slug}",
        "summary": f"What the system does about {slug}.",
        "status": "considering",
        "record": record,
        "synthesizes": ["[[paper.a]]"],
        **fields,
    }
    sections = [{"heading": heading, "level": 2, "body": f"{heading} of {slug}, citing [[paper.a]]."} for heading in SECTIONS]
    return {
        "page_id": f"stance.{slug}",
        "path": f"Wiki/Stances/{slug}.md",
        "payload": {"frontmatter": frontmatter, "sections": sections, "claims": []},
    }


@dataclass
class Bases:
    """The records base and the research base the programs read and write, scripted.

    `records` holds each records page's frontmatter by id, and `stances` each stance page as read_page answers it.
    A records page missing from `records` is one read_page cannot find and list_pages does not list. `outside_root`
    names records pages whose file lies in an app's own docs folder, beyond the records base's root, which list_pages
    lists and read_page refuses the way the real tool does. `unreadable` names stance ids list_pages lists and
    read_page refuses. `refusing` names the stance ids write_note refuses. `writes`, `listed` and `reads` record every
    write_note, list_pages and read_page call, in order.
    """

    records: dict[str, dict[str, str]] = field(default_factory=dict)
    stances: dict[str, dict[str, object]] = field(default_factory=dict)
    outside_root: frozenset[str] = frozenset()
    unreadable: frozenset[str] = frozenset()
    refusing: frozenset[str] = frozenset()
    writes: list[WriteCall] = field(default_factory=list)
    listed: list[dict[str, object]] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)

    async def call(self, name: str, args: dict[str, object], /) -> object:
        if name == "read_page":
            self.reads.append((str(args["knowledge_base"]), str(args["page_id"])))
            return self._read(str(args["knowledge_base"]), str(args["page_id"]))
        if name == "list_pages":
            self.listed.append(dict(args))
            return self._list(str(args["knowledge_base"]), str(args["page_type"]), str(args.get("status", "")))
        if name == "write_note":
            write: WriteCall = to_jsonable_python(args)
            self.writes.append(write)
            page_id = str(write["payload"]["frontmatter"]["id"])
            if page_id in self.refusing:
                msg = "the page was refused by research's grammar:\nstatus 'settled' is not one of considering, trying, adopted, rejected"
                raise ModelRetry(msg)
            return f"{page_id} written to {write['path']}, indexed clean"
        msg = f"no program in this workflow calls {name!r}"
        raise AssertionError(msg)

    def _read(self, knowledge_base: str, page_id: str) -> object:
        if knowledge_base == "repo-docs" and page_id in self.outside_root:
            msg = f"path '../harness/docs/adr/{page_id}.md' resolves outside the knowledge base root"
            raise ModelRetry(msg)
        if knowledge_base == "repo-docs" and page_id in self.records:
            payload = {
                "frontmatter": self.records[page_id],
                "sections": [{"heading": "Decision", "level": 2, "body": "What was decided."}],
                "claims": [],
            }
            return {"page_id": page_id, "path": f"adr/{page_id}.md", "payload": payload}
        if knowledge_base == "research" and page_id in self.stances:
            return self.stances[page_id]
        if knowledge_base == "research" and page_id in self.unreadable:
            msg = (
                f"{page_id} at Wiki/Stances/{page_id.removeprefix('stance.')}.md cannot be read as a payload: text before the first heading"
            )
            raise ModelRetry(msg)
        msg = f"no indexed page {page_id!r} in knowledge base {knowledge_base!r}, list_pages and search answer the page ids it holds"
        raise ModelRetry(msg)

    def _list(self, knowledge_base: str, page_type: str, status: str) -> list[dict[str, str]]:
        if knowledge_base == "research":
            assert page_type == "stance"
            return [
                {"page_id": stance_id, "page_type": "stance", "title": stance_id, "status": "considering"}
                for stance_id in [*self.stances, *sorted(self.unreadable)]
            ]
        return [
            {"page_id": page_id, "page_type": front["type"], "title": front["title"], "status": front["status"]}
            for page_id, front in self.records.items()
            if front["type"] == page_type and front["status"] == status
        ]


def _skip_all(group: Group) -> list[dict[str, str]]:
    """Every page of the group skipped, as a placement worker answers for pages that change no stance."""
    return [
        {"page_id": page["page_id"], "placement": "skip", "stance_id": "", "title": "", "reason": "It changes no stance."}
        for page in group["pages"]
    ]


def _rewritten(item: Item, *, changing: str = "Position") -> dict[str, object]:
    """The draft a refresh worker answers: the stance moved to trying, with one section rewritten from its first page."""
    resting_on = item["pages"][0]["page_id"]
    current = item["current"]
    if current is None:
        frontmatter: dict[str, object] = {
            "id": item["stance_id"],
            "type": "stance",
            "title": item["title"],
            "summary": "What the system plans on this topic.",
            "status": "trying",
            "record": resting_on,
        }
        sections = [
            Section(heading=heading, level=2, body=f"{heading} of the new stance, resting on `{resting_on}`.") for heading in SECTIONS
        ]
    else:
        frontmatter = dict(current["frontmatter"], status="trying")
        sections = [
            Section(heading=section["heading"], level=section["level"], body=f"Rewritten, resting on `{resting_on}`.")
            if section["heading"] == changing
            else section
            for section in current["sections"]
        ]
    return {
        "stance_id": item["stance_id"],
        "path": item["path"],
        "new": item["new"],
        "payload": {"frontmatter": frontmatter, "sections": sections},
        "sources": [page["page_id"] for page in item["pages"]],
        "findings_left": [],
    }


@dataclass
class Scripted:
    """The three model steps of the workflow, scripted. The programs run for real.

    `place` answers each group's placements and `rewrite` each item's draft. The judge approves whatever it is
    handed. `calls` records every node performed, in order, and `groups`, `items` and `reviewed` what each
    placement worker, refresh worker and judge call was handed.
    """

    place: Callable[[Group], list[dict[str, str]]] = _skip_all
    rewrite: Callable[[Item], dict[str, object]] = _rewritten
    calls: list[str] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    reviewed: list[list[object]] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append(node_id)
        if node_id == "place_pages":
            group: Group = to_jsonable_python(kwargs["group"])
            self.groups.append(group)
            return _typed(implementation, {"placements": self.place(group)})
        if node_id == "refresh_stances":
            item: Item = to_jsonable_python(kwargs["item"])
            self.items.append(item)
            return _typed(implementation, self.rewrite(item))
        if node_id == "check_stances":
            self.reviewed.append(to_jsonable_python(kwargs["drafts"]))
            return _typed(implementation, {"outcome": "approve", "feedback": ""})
        return await implementation.function(**kwargs)


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


async def _registry(sandbox: AsyncMonty, bases: Bases) -> StepRegistry:
    """A registry offering exactly what this app offers a declared step, with the programs' tools answered by `bases`."""
    grantable = await app_grants()
    return StepRegistry(
        perform=_never_performed,
        grants=grantable,
        run_code=code_runner(sandbox, reads=await app_reads(), call=bases.call),
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )


async def _run(sandbox: AsyncMonty, bases: Bases, scripted: Scripted, *, changed: str, progress: str = PROGRESS) -> WorkflowState:
    """One whole run of the shipped workflow, as the schedule starts it with `changed` as its changed page ids."""
    params = {"collection": "research", "records": "repo-docs", "progress": progress, "changed_page_ids": changed}
    state = WorkflowState(params=dict(params))
    graph = build_graph(_spec(), await _registry(sandbox, bases), approval=NoPerson(), executor=scripted)
    await graph.run(state=state)
    return state


def _value(state: WorkflowState, node_id: str) -> dict[str, object]:
    """One node's recorded value as plain data."""
    value = to_jsonable_python(state.values[node_id])
    assert isinstance(value, dict)
    return value


def _outcome(state: WorkflowState) -> str:
    """The checked outcome of a completed run, read off its own facts the way the run card reads it."""
    ended = EndedRun(
        spec=_spec(),
        run_id="run-stance-refresh",
        status="completed",
        values=state.values,
        params=state.params,
        work_facts=list(state.work_facts.values()),
        batches=state.batches,
        gate_decisions=state.gate_decisions,
        gate_generations=state.gate_generations,
    )
    return checked_outcome(ended).status


async def test_a_stance_whose_record_changed_is_rewritten_and_written(sandbox: AsyncMonty) -> None:
    stance = _stance("x", record="adr-x")
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": stance})
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-x")

    assert scripted.groups == [], "a page a stance names as its record needs no placement"
    assert [item["pages"] for item in scripted.items] == [[_listed("adr-x")]]
    assert [(write["knowledge_base"], write["path"]) for write in bases.writes] == [("research", "Wiki/Stances/x.md")]
    read: Payload = to_jsonable_python(stance["payload"])
    assert bases.writes[0]["payload"]["frontmatter"] == dict(read["frontmatter"], status="trying"), "no field the page lacks is written"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
    kept: list[StanceAsRead] = to_jsonable_python(_value(state, "list_changes")["stances"])
    assert [
        (stance["stance_id"], stance["path"], stance["payload"]["frontmatter"]["status"], stance["payload"]["sections"]) for stance in kept
    ] == [("stance.x", "Wiki/Stances/x.md", "considering", read["sections"])], "the page as it was before the run stays on the run"


async def test_a_rewrite_that_alters_evidence_is_not_written(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": _stance("x", record="adr-x")})

    def alter_evidence(item: Item) -> dict[str, object]:
        return _rewritten(item, changing="Evidence")

    scripted = Scripted(rewrite=alter_evidence)

    state = await _run(sandbox, bases, scripted, changed="adr-x")

    assert scripted.calls.count("check_stances") == 1, "the judge approved the rewrite"
    assert bases.writes == []
    failed: list[dict[str, str]] = to_jsonable_python(_value(state, "write_stances")["failed"])
    assert [entry["stance_id"] for entry in failed] == ["stance.x"]
    assert "Evidence" in failed[0]["reason"]


async def test_a_write_note_refusal_is_reported_not_fatal(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-y": _record("adr-y")},
        stances={"stance.x": _stance("x", record="adr-x"), "stance.y": _stance("y", record="adr-y")},
        refusing=frozenset({"stance.y"}),
    )

    state = await _run(sandbox, bases, Scripted(), changed="adr-x,adr-y")

    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md", "Wiki/Stances/y.md"]
    result = _value(state, "write_stances")
    assert result["written"] == ["stance.x"]
    failed: list[dict[str, str]] = to_jsonable_python(result["failed"])
    assert [entry["stance_id"] for entry in failed] == ["stance.y"]
    assert "status 'settled' is not one of" in failed[0]["reason"], "the refusal's own words reach the run card"


async def test_more_than_twenty_five_admitted_pages_keep_none(sandbox: AsyncMonty) -> None:
    records = {f"adr-{number}": _record(f"adr-{number}") for number in range(1, 27)}
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-1")})
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed=",".join(records))

    changes = _value(state, "list_changes")
    assert changes["capped"] is True
    assert changes["qualified"] == 26
    assert changes["admitted"] == []
    assert "place_pages" not in scripted.calls
    assert "refresh_stances" not in scripted.calls
    assert bases.writes == []
    assert _outcome(state) == "noop"


async def test_a_run_that_admits_nothing_ends_noop(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-p": _record("adr-p", status="proposed")}, stances={"stance.x": _stance("x", record="adr-p")})
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-p")

    assert _value(state, "list_changes")["admitted"] == []
    assert scripted.reviewed == [[]], "one judge call, on an empty list"
    assert scripted.calls.count("check_stances") == 1
    assert bases.writes == []
    assert _outcome(state) == "noop"


async def test_placements_proposing_the_same_new_stance_make_one_item(sandbox: AsyncMonty) -> None:
    records = {f"adr-{number}": _record(f"adr-{number}") for number in range(1, 7)}
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-other")})

    def propose_agent_memory(group: Group) -> list[dict[str, str]]:
        first, *rest = group["pages"]
        proposed = {
            "page_id": first["page_id"],
            "placement": "new",
            "stance_id": "stance.agent-memory",
            "title": "Agent memory",
            "reason": "No stance covers it.",
        }
        return [proposed, *_skip_all(Group(group_id=group["group_id"], pages=rest))]

    scripted = Scripted(place=propose_agent_memory)

    state = await _run(sandbox, bases, scripted, changed=",".join(records))

    assert sorted(len(group["pages"]) for group in scripted.groups) == [1, 5], "the six pages go out in groups of five"
    assert scripted.items == [
        {
            "stance_id": "stance.agent-memory",
            "title": "Agent memory",
            "path": "",
            "new": True,
            "pages": [_listed("adr-1"), _listed("adr-6")],
            "current": None,
        }
    ]
    assert [(write["path"], write["payload"]["frontmatter"]["id"]) for write in bases.writes] == [("", "stance.agent-memory")]
    assert _value(state, "write_stances") == {"written": ["stance.agent-memory"], "failed": []}


async def test_a_stance_whose_record_is_missing_is_left_alone(sandbox: AsyncMonty) -> None:
    bases = Bases(records={}, stances={"stance.x": _stance("x", record="adr-x")})
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-x")

    assert _value(state, "list_changes")["admitted"] == []
    assert scripted.items == []
    assert bases.writes == []
    assert _outcome(state) == "noop"


async def test_a_changed_page_outside_the_records_root_is_admitted_through_list_pages(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-0049-rerank": _record("adr-0049-rerank")},
        stances={"stance.x": _stance("x", record="adr-0049-rerank")},
        outside_root=frozenset({"adr-0049-rerank"}),
    )

    state = await _run(sandbox, bases, Scripted(), changed="adr-0049-rerank")

    assert _value(state, "list_changes")["admitted"] == [_listed("adr-0049-rerank")]
    assert [page_id for knowledge_base, page_id in bases.reads if knowledge_base == "repo-docs"] == [], "no records page file is read"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


@pytest.mark.parametrize("changed", ["adr-x", ""])
async def test_a_repeated_pair_is_listed_once(sandbox: AsyncMonty, changed: str) -> None:
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": _stance("x", record="adr-other")})

    state = await _run(sandbox, bases, Scripted(), changed=changed, progress="adr:accepted, adr: accepted ,adr:accepted")

    assert _value(state, "list_changes")["admitted"] == [_listed("adr-x")]
    listed = [(call["page_type"], call["status"]) for call in bases.listed if call["knowledge_base"] == "repo-docs"]
    assert listed == [("adr", "accepted")]


async def test_a_full_pass_lists_every_pair_and_keeps_past_twenty_five(sandbox: AsyncMonty) -> None:
    records = {f"adr-{number}": _record(f"adr-{number}") for number in range(1, 28)}
    records["spec-a"] = _record("spec-a", status="executed", page_type="spec-doc")
    records["adr-draft"] = _record("adr-draft", status="proposed")
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-other")})

    state = await _run(sandbox, bases, Scripted(), changed="")

    changes = _value(state, "list_changes")
    assert (changes["qualified"], changes["capped"]) == (28, False)
    listed = [(call["page_type"], call["status"]) for call in bases.listed if call["knowledge_base"] == "repo-docs"]
    assert listed == [
        ("adr", "accepted"),
        ("adr", "superseded"),
        ("architecture", "current"),
        ("spec-doc", "approved"),
        ("spec-doc", "executed"),
    ]


async def test_an_unreadable_stance_is_named_and_another_stance_is_still_refreshed(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-b": _record("adr-b")},
        stances={"stance.x": _stance("x", record="adr-x")},
        unreadable=frozenset({"stance.broken"}),
    )

    def place_on_the_unread_stance(group: Group) -> list[dict[str, str]]:
        return [
            {
                "page_id": page["page_id"],
                "placement": "stance",
                "stance_id": "stance.broken",
                "title": "Broken",
                "reason": "It changes that stance.",
            }
            for page in group["pages"]
        ]

    scripted = Scripted(place=place_on_the_unread_stance)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-b")

    changes = _value(state, "list_changes")
    assert changes["stance_ids"] == ["stance.x", "stance.broken"]
    unread: list[dict[str, str]] = to_jsonable_python(changes["unread"])
    assert [(entry["stance_id"], entry["record"]) for entry in unread] == [("stance.broken", "")]
    assert "cannot be read as a payload" in unread[0]["reason"], "the refusal's own words reach the run card"
    assert [item["stance_id"] for item in scripted.items] == ["stance.x"], "a placement on a stance the run could not read makes no item"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


async def test_a_stance_with_an_undeclared_frontmatter_field_is_named_and_the_run_succeeds(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-t": _record("adr-t")}, stances={"stance.tagged": _stance("tagged", record="adr-t", tags=["retrieval"])})
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-t")

    changes = _value(state, "list_changes")
    assert changes["stances"] == []
    assert changes["stance_ids"] == ["stance.tagged"]
    unread: list[dict[str, str]] = to_jsonable_python(changes["unread"])
    assert [(entry["stance_id"], entry["record"]) for entry in unread] == [("stance.tagged", "adr-t")]
    assert "tags" in unread[0]["reason"]
    assert scripted.groups == [], "the page an unread stance names as its record is not placed as uncovered"
    assert scripted.items == []
    assert bases.writes == []
    assert _outcome(state) == "noop"


async def test_a_new_draft_taking_an_existing_stance_id_is_never_written(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, unreadable=frozenset({"stance.broken"})
    )

    def propose_the_unread_id(group: Group) -> list[dict[str, str]]:
        return [
            {
                "page_id": page["page_id"],
                "placement": "new",
                "stance_id": "stance.broken",
                "title": "Broken",
                "reason": "No stance covers it.",
            }
            for page in group["pages"]
        ]

    scripted = Scripted(place=propose_the_unread_id)

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert [(item["stance_id"], item["new"]) for item in scripted.items] == [("stance.broken", True)]
    assert bases.writes == []
    failed: list[dict[str, str]] = to_jsonable_python(_value(state, "write_stances")["failed"])
    assert [entry["stance_id"] for entry in failed] == ["stance.broken"]
    assert "existing stance stance.broken" in failed[0]["reason"]


async def test_a_new_draft_is_written_where_its_page_type_places_it(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")})

    def propose_agent_memory(group: Group) -> list[dict[str, str]]:
        return [
            {
                "page_id": page["page_id"],
                "placement": "new",
                "stance_id": "stance.agent-memory",
                "title": "Agent memory",
                "reason": "No stance covers it.",
            }
            for page in group["pages"]
        ]

    def aim_at_an_existing_file(item: Item) -> dict[str, object]:
        return {**_rewritten(item), "path": "Wiki/Stances/x.md"}

    state = await _run(sandbox, bases, Scripted(place=propose_agent_memory, rewrite=aim_at_an_existing_file), changed="adr-1")

    assert [(write["path"], write["payload"]["frontmatter"]["id"]) for write in bases.writes] == [("", "stance.agent-memory")]
    assert _value(state, "write_stances") == {"written": ["stance.agent-memory"], "failed": []}


async def test_a_finding_left_in_unchanged_evidence_reaches_the_judge_and_the_page_still_lands(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": _stance("x", record="adr-x")})
    left = "Evidence links [[paper.gone]], which no page of the knowledge base carries"

    def leave_the_evidence_finding(item: Item) -> dict[str, object]:
        return {**_rewritten(item), "findings_left": [left]}

    scripted = Scripted(rewrite=leave_the_evidence_finding)

    state = await _run(sandbox, bases, scripted, changed="adr-x")

    reviewed: list[dict[str, object]] = to_jsonable_python(scripted.reviewed[0])
    assert [draft["findings_left"] for draft in reviewed] == [[left]], "the judge reads the finding the worker left"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
