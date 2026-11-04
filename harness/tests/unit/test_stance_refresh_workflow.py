"""The shipped stance-refresh workflow, run end to end off its own files.

The graph and the steps come from the real documents under `templates/research/workflows`, never from a copy typed
out here. The five model steps are scripted and no model is reached. The six programs run for real in the
sandbox, and every tool they call answers from a scripted records base and a scripted research base, so nothing
reaches a real knowledge base.

What the cases cover: a stance whose record changed is rewritten and written to its own path, a refresh answer that
names Evidence or Alternatives, or carries no changes, fails the refresh step's own output type, the merge keeps every
section, frontmatter field and claim the draft does not name exactly as read, a draft naming a section twice or one
the page lacks is refused, a refused write is reported beside the written one, more than twenty-five changed pages
keep none, a run that admits nothing makes one judge call on an empty list and ends noop, a changed page the records
base does not list leaves its stance alone, a changed page whose file lies outside the records base's root is still
admitted, a repeated pair is listed once, a full pass lists every pair with no cap, and the refresh worker is granted
read_page to patch the stance it reads. Then the stances the run cannot read: one read_page refuses and one carrying a
field the workflow does not declare are both named and left alone while the run carries on, while one carrying the
`created` date write_note stamps is read and written back with it. Then the new stances placement proposes: placement
names a proposal by its id, title and pages alone, no worker drafts one and no page is written for one, each reaches
write_stances' `proposed` and the `proposed_stances` artifact with its pages and its findings, two groups proposing
the same id make one proposal, and an id a listed stance carries makes no proposal. Then the findings behind a
proposal: each keeps the claim's own text copied from its digest, a missing anchor, a skipped digest, a page that is
no digest and one digest cited twice never count toward the two digests, a digest read_page refuses drops only its own
findings while the run still writes its rewrites, a proposal whose findings all contest it is kept and marked, a
rejection by the proposals judge backs the proposals again and never reruns the rewrites, and a run with no proposal
backs nothing and reads no digest. Last, the budgets of the backing worker and the rewrite judge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest
import yaml

from core.ai.workflows.contracts import (
    Briefing,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowState,
    build_graph,
    canonical_reviewed_value,
    load_spec_document,
    load_step_document,
    reviewed_value_digest,
    step_page_id,
)
from pydantic import ValidationError
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

    from core.ai.workflows.contracts import InlineStep, Outcome
    from core.ai.workflows.registry import StepImplementation

RESEARCH = Path(__file__).resolve().parents[3] / "templates" / "research" / "workflows"

PROGRESS = "adr:accepted, adr:superseded, architecture:current, spec-doc:approved, spec-doc:executed"
"""The pairs the schedule passes, the same for every case here."""

SECTIONS = ("TL;DR", "Position", "Why", "Evidence", "Alternatives", "Open Questions")
"""The stance page type's canonical sections, headed the way read_page heads them."""

HOW = "It measures the position directly."
"""The sentence every scripted backing worker gives on how a claim bears."""


@dataclass(frozen=True)
class Digest:
    """One page of the scripted research base that read_page answers with claims: its type, its status and its claims by anchor."""

    page_type: str
    status: str
    claims: dict[str, str]


DIGESTS = {
    "paper.dense-retrieval": Digest(
        "paper",
        "active",
        {"^c1": "Dense retrieval beats keyword search on open questions.", "^c2": "Dense retrieval needs in-domain training data."},
    ),
    "art.memory-survey": Digest("article", "active", {"^c3": "Agents that keep episodic memory finish more long tasks."}),
    "talk.agent-loops": Digest("talk", "active", {"^c4": "Agent loops without memory repeat the actions that failed."}),
    "post.keep-shipping": Digest("post", "skipped", {"^c1": "Small changes shipped often break less."}),
    "con.memory": Digest("concept", "active", {"^c1": "Memory comes in episodic and semantic kinds."}),
}
"""The research base's pages read_page answers with claims: four digests, one of them skipped, and a concept page that is no digest."""


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
    pages: list[Page]
    current: Payload


class Proposal(TypedDict):
    stance_id: str
    title: str
    pages: list[Page]


class Finding(TypedDict):
    claim: str
    side: str
    how: str


class StanceAsRead(TypedDict):
    stance_id: str
    path: str
    payload: Payload


class PayloadAsRead(Payload):
    claims: list[dict[str, str]]


class PageAsRead(TypedDict):
    page_id: str
    path: str
    payload: PayloadAsRead


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
    `digests` holds the research pages read_page answers with claims, and `refused_digests` names the ids among them
    read_page refuses. A research id held by none of them is one read_page cannot find. A records page missing from `records` is one read_page cannot find and list_pages does not list. `outside_root`
    names records pages whose file lies in an app's own docs folder, beyond the records base's root, which list_pages
    lists and read_page refuses the way the real tool does. `unreadable` names stance ids list_pages lists and
    read_page refuses. `refusing` names the stance ids write_note refuses. `writes`, `listed` and `reads` record every
    write_note, list_pages and read_page call, in order.
    """

    records: dict[str, dict[str, str]] = field(default_factory=dict)
    stances: dict[str, dict[str, object]] = field(default_factory=dict)
    digests: dict[str, Digest] = field(default_factory=dict)
    refused_digests: frozenset[str] = frozenset()
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
        if knowledge_base == "research" and page_id in self.refused_digests:
            msg = f"{page_id} at Wiki/{page_id}.md cannot be read as a payload: claim ^c1 does not read back"
            raise ModelRetry(msg)
        if knowledge_base == "research" and page_id in self.digests:
            digest = self.digests[page_id]
            frontmatter = {"id": page_id, "type": digest.page_type, "title": f"Title of {page_id}", "status": digest.status}
            claims = [{"anchor": anchor, "text": text, "loc": "p. 1"} for anchor, text in digest.claims.items()]
            payload = {"frontmatter": frontmatter, "sections": [{"heading": "Findings", "level": 2, "body": ""}], "claims": claims}
            return {"page_id": page_id, "path": f"Wiki/{page_id}.md", "payload": payload}
        msg = f"no indexed page {page_id!r} in knowledge base {knowledge_base!r}, list_pages and search answer the page ids it holds"
        raise ModelRetry(msg)

    def _list(self, knowledge_base: str, page_type: str, status: str) -> list[dict[str, str]]:
        if knowledge_base == "research" and page_type == "stance":
            return [
                {"page_id": stance_id, "page_type": "stance", "title": stance_id, "status": "considering"}
                for stance_id in [*self.stances, *sorted(self.unreadable)]
            ]
        assert knowledge_base == "repo-docs", f"no program in this workflow lists the {page_type!r} pages of {knowledge_base!r}"
        return [
            {"page_id": page_id, "page_type": front["type"], "title": front["title"], "status": front["status"]}
            for page_id, front in self.records.items()
            if front["type"] == page_type and front["status"] == status
        ]


def _skip_all(group: Group) -> list[dict[str, object]]:
    """Every page of the group skipped, as a placement worker answers for pages that change no stance."""
    return [
        {"page_id": page["page_id"], "placement": "skip", "stance_id": "", "title": "", "reason": "It changes no stance."}
        for page in group["pages"]
    ]


def _finding(claim: str, side: str = "supports") -> Finding:
    """One finding a backing worker answers: a claim reference, its side and the sentence on how it bears."""
    return Finding(claim=claim, side=side, how=HOW)


BACKED = [_finding("paper.dense-retrieval#^c1"), _finding("art.memory-survey#^c3")]
"""Two findings from two digests the research base holds, what a backing worker answers unless a case says otherwise."""


def _backed_by_two(_: Proposal) -> list[Finding]:
    """The findings of BACKED, for every proposal."""
    return BACKED


def _shown(claim: str, side: str = "supports") -> dict[str, str]:
    """One finding as check_findings keeps it, with the claim's own text copied from its digest."""
    page_id, anchor = claim.split("#")
    return {"claim": claim, "claim_text": DIGESTS[page_id].claims[anchor], "side": side, "how": HOW}


def _kept(stance_id: str, title: str, pages: list[Page], claims: list[str], *, contests_only: bool = False) -> dict[str, object]:
    """One proposal as check_findings keeps it, every finding on its claims supporting it unless `contests_only`."""
    side = "contests" if contests_only else "supports"
    return {
        "stance_id": stance_id,
        "title": title,
        "pages": pages,
        "findings": [_shown(claim, side) for claim in claims],
        "contests_only": contests_only,
    }


MEMORY = _kept("stance.agent-memory", "Agent memory", [_listed("adr-1")], ["paper.dense-retrieval#^c1", "art.memory-survey#^c3"])
"""The proposal stance.agent-memory as the run keeps it, proposed for adr-1 alone and backed by BACKED."""


def _rewritten(item: Item, *, changing: str = "Position") -> dict[str, object]:
    """The draft a refresh worker answers: the stance moved to trying, with one section rewritten from its first page.

    The draft carries only its changes, its summary kept as read.
    """
    resting_on = item["pages"][0]["page_id"]
    changed = Section(heading=changing, level=2, body=f"Rewritten, resting on `{resting_on}`.")
    return {
        "stance_id": item["stance_id"],
        "path": item["path"],
        "sources": [page["page_id"] for page in item["pages"]],
        "findings_left": [],
        "changes": {"status": "trying", "summary": str(item["current"]["frontmatter"]["summary"]), "sections": [changed]},
    }


@dataclass
class Scripted:
    """The five model steps of the workflow, scripted. The programs run for real.

    `place` answers each group's placements, `rewrite` each item's draft and `back` each proposal's findings. The
    rewrite judge approves whatever it is handed. The proposals judge answers `proposal_verdicts` in order, then
    approves. `calls` records every node performed, in order, and `groups`, `items`, `reviewed`, `backed` and `judged`
    what each placement worker, refresh worker, rewrite judge call, backing worker and proposals judge call was handed.
    """

    place: Callable[[Group], list[dict[str, object]]] = _skip_all
    rewrite: Callable[[Item], dict[str, object]] = _rewritten
    back: Callable[[Proposal], list[Finding]] = _backed_by_two
    proposal_verdicts: list[dict[str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    reviewed: list[list[object]] = field(default_factory=list)
    backed: list[Proposal] = field(default_factory=list)
    judged: list[list[object]] = field(default_factory=list)

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
        if node_id == "back_proposals":
            proposal: Proposal = to_jsonable_python(kwargs["proposal"])
            self.backed.append(proposal)
            return _typed(implementation, {**proposal, "findings": self.back(proposal)})
        if node_id == "judge_proposals":
            self.judged.append(to_jsonable_python(kwargs["proposals"]))
            verdict = self.proposal_verdicts.pop(0) if self.proposal_verdicts else {"outcome": "approve", "feedback": ""}
            return _typed(implementation, verdict)
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


def _checked(state: WorkflowState) -> Outcome:
    """The checked outcome of a completed run with its minted artifacts, read off its own facts the way the run card reads it."""
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
    return checked_outcome(ended)


def _outcome(state: WorkflowState) -> str:
    """The checked outcome's status of a completed run."""
    return _checked(state).status


async def test_the_refresh_worker_is_granted_read_page_to_patch_the_stance_in_code() -> None:
    refresh = next(step for step in _spec().inline if step.name == "refresh_stance")
    assert isinstance(refresh, Briefing)
    assert {"read_page", "check_note"} <= set(refresh.tools)
    assert set(refresh.tools) <= set(await app_grants()), "every tool the refresh worker names is one this app grants a step"
    assert "read_page(" in refresh.instructions, "the worker is told to load the stance inside run_code rather than retype it"


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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}
    kept: list[StanceAsRead] = to_jsonable_python(_value(state, "list_changes")["stances"])
    assert [
        (stance["stance_id"], stance["path"], stance["payload"]["frontmatter"]["status"], stance["payload"]["sections"]) for stance in kept
    ] == [("stance.x", "Wiki/Stances/x.md", "considering", read["sections"])], "the page as it was before the run stays on the run"


@pytest.mark.parametrize("heading", ["Evidence", "Alternatives"])
async def test_a_draft_naming_evidence_or_alternatives_fails_the_refresh_output_type(sandbox: AsyncMonty, heading: str) -> None:
    registry, problems = (await _registry(sandbox, Bases())).including(_spec())
    assert problems == ()
    refresh = registry.get("refresh_stance")
    assert refresh is not None
    stance: PageAsRead = to_jsonable_python(_stance("x", record="adr-x"))
    item = Item(stance_id="stance.x", title="Stance on x", path="Wiki/Stances/x.md", pages=[_listed("adr-x")], current=stance["payload"])
    _typed(refresh, _rewritten(item, changing="Position"))

    with pytest.raises(ValidationError, match=r"changes\.sections\.0\.heading"):
        _typed(refresh, _rewritten(item, changing=heading))


async def test_the_merge_keeps_every_part_the_draft_does_not_name_exactly_as_read(sandbox: AsyncMonty) -> None:
    stance = _stance(
        "x",
        record="adr-x",
        recommends=["[[ent.a]]"],
        advises_against=["[[ent.b]]"],
        aliases=["x stance"],
        decision_reason="Chosen in review.",
    )
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": stance})

    def rewrite_two_sections(item: Item) -> dict[str, object]:
        sections = [
            Section(heading="Open Questions", level=3, body="Answered by `adr-x`."),
            Section(heading="TL;DR", level=2, body="Adopted, resting on `adr-x`."),
        ]
        return {**_rewritten(item), "changes": {"status": "adopted", "summary": "What the system does now.", "sections": sections}}

    state = await _run(sandbox, bases, Scripted(rewrite=rewrite_two_sections), changed="adr-x")

    read: PageAsRead = to_jsonable_python(stance)
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"]
    written = bases.writes[0]["payload"]
    assert written["frontmatter"] == dict(read["payload"]["frontmatter"], status="adopted", summary="What the system does now.")
    replaced = {"TL;DR": "Adopted, resting on `adr-x`.", "Open Questions": "Answered by `adr-x`."}
    assert written["sections"] == [
        Section(heading=section["heading"], level=section["level"], body=replaced.get(section["heading"], section["body"]))
        for section in read["payload"]["sections"]
    ], "the named sections take the draft's body at the level and place the page gave them, and every other section stays as read"
    assert to_jsonable_python(written).get("claims", []) == read["payload"]["claims"] == [], "a stance takes no claims and none is written"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


@pytest.mark.parametrize(
    ("headings", "reason"),
    [
        (["Position", "Position"], "changes the Position section twice"),
        (["Open Questions"], "the Open Questions section, which the page as read before this run does not carry"),
    ],
)
async def test_a_draft_naming_a_section_twice_or_one_the_page_lacks_is_refused(
    sandbox: AsyncMonty, headings: list[str], reason: str
) -> None:
    stance = _stance("x", record="adr-x")
    payload: Payload = to_jsonable_python(stance["payload"])
    stance["payload"] = {**payload, "sections": [section for section in payload["sections"] if section["heading"] != "Open Questions"]}
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": stance})

    def change(item: Item) -> dict[str, object]:
        sections = [Section(heading=heading, level=2, body="Rewritten, resting on `adr-x`.") for heading in headings]
        return {**_rewritten(item), "changes": {"status": "trying", "summary": "What the system plans.", "sections": sections}}

    state = await _run(sandbox, bases, Scripted(rewrite=change), changed="adr-x")

    assert bases.writes == []
    failed: list[dict[str, str]] = to_jsonable_python(_value(state, "write_stances")["failed"])
    assert [entry["stance_id"] for entry in failed] == ["stance.x"]
    assert reason in failed[0]["reason"]


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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


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

    def place_on_the_unread_stance(group: Group) -> list[dict[str, object]]:
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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


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


async def test_a_stance_carrying_created_is_read_and_written_back_with_it(sandbox: AsyncMonty) -> None:
    stance = _stance("x", record="adr-x", created="2026-10-29")
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": stance})

    state = await _run(sandbox, bases, Scripted(), changed="adr-x")

    changes = _value(state, "list_changes")
    assert changes["unread"] == [], "the date write_note stamps on every page it writes keeps no page out of the next run"
    kept: list[StanceAsRead] = to_jsonable_python(changes["stances"])
    assert [stance["stance_id"] for stance in kept] == ["stance.x"]
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"]
    assert bases.writes[0]["payload"]["frontmatter"]["created"] == "2026-10-29", "the page keeps the date it was first written on"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


def _proposing(stance_id: str, title: str, *, first_only: bool = False) -> Callable[[Group], list[dict[str, object]]]:
    """A placement worker proposing the new stance `stance_id` for every page of its group, or for its first page alone."""

    def place(group: Group) -> list[dict[str, object]]:
        proposed = group["pages"][:1] if first_only else group["pages"]
        rest = group["pages"][len(proposed) :]
        placements: list[dict[str, object]] = [
            {"page_id": page["page_id"], "placement": "new", "stance_id": stance_id, "title": title, "reason": "Papers argue for it."}
            for page in proposed
        ]
        return [*placements, *_skip_all(Group(group_id=group["group_id"], pages=rest))]

    return place


def test_placement_names_a_new_stance_by_its_id_title_and_pages_alone() -> None:
    place = next(step for step in _spec().inline if step.name == "place_stance_pages")
    assert isinstance(place, Briefing)
    assert isinstance(place.output, Shape)
    placement = place.output.json_schema["properties"]["placements"]["items"]
    assert set(placement["properties"]) == {"page_id", "placement", "stance_id", "title", "reason"}, "a placement cites no digest"
    assert "research_pages" not in place.instructions


async def test_a_proposed_new_stance_makes_no_rewrite_and_reaches_the_output_with_its_findings(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert scripted.items == [], "no worker drafts a proposed stance"
    assert scripted.backed == [Proposal(stance_id="stance.agent-memory", title="Agent memory", pages=[_listed("adr-1")])]
    assert bases.writes == []
    assert _value(state, "write_stances") == {"written": [], "failed": [], "proposed": [MEMORY]}


async def test_a_proposal_only_run_carries_its_judged_proposals_as_an_artifact_and_ends_noop(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert scripted.judged == [[MEMORY]], "the proposals judge reads every finding with the claim's own text"
    assert to_jsonable_python(state.values["judge_proposals"]) == [MEMORY]
    outcome = _checked(state)
    assert outcome.status == "noop", "no stance was rewritten"
    assert [(ref.id, ref.node_id, ref.media_type, ref.value_digest) for ref in outcome.artifacts] == [
        ("proposed_stances", "judge_proposals", "application/json", reviewed_value_digest(canonical_reviewed_value([MEMORY])))
    ], "the card shows each approved proposal with its title, its pages and every finding's claim, text, side and sentence"


async def test_proposals_of_the_same_id_combine_into_one_with_every_page(sandbox: AsyncMonty) -> None:
    records = {f"adr-{number}": _record(f"adr-{number}") for number in range(1, 7)}
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory", first_only=True))

    state = await _run(sandbox, bases, scripted, changed=",".join(records))

    assert sorted(len(group["pages"]) for group in scripted.groups) == [1, 5], "the six pages go out in groups of five"
    assert scripted.items == []
    assert [proposal["pages"] for proposal in scripted.backed] == [[_listed("adr-1"), _listed("adr-6")]], "one worker backs the proposal"
    assert _value(state, "write_stances")["proposed"] == [{**MEMORY, "pages": [_listed("adr-1"), _listed("adr-6")]}]


async def test_write_stances_writes_each_rewrite_and_never_a_page_for_a_proposal(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"))

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    assert [item["stance_id"] for item in scripted.items] == ["stance.x"]
    assert [(write["path"], write["payload"]["frontmatter"]["id"]) for write in bases.writes] == [("Wiki/Stances/x.md", "stance.x")]
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": [MEMORY]}


async def test_a_proposal_under_a_stance_the_run_read_joins_that_stance_rewrite(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")})
    scripted = Scripted(place=_proposing("stance.x", "Stance on x"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert [(item["stance_id"], item["pages"]) for item in scripted.items] == [("stance.x", [_listed("adr-1")])]
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


async def test_a_proposal_under_a_stance_the_run_could_not_read_is_neither_refreshed_nor_proposed(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, unreadable=frozenset({"stance.broken"})
    )
    scripted = Scripted(place=_proposing("stance.broken", "Broken"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert scripted.items == []
    assert bases.writes == []
    assert _value(state, "write_stances") == {"written": [], "failed": [], "proposed": []}


async def test_a_draft_without_its_changes_fails_the_refresh_output_type(sandbox: AsyncMonty) -> None:
    registry, problems = (await _registry(sandbox, Bases())).including(_spec())
    assert problems == ()
    refresh = registry.get("refresh_stance")
    assert refresh is not None
    stance: PageAsRead = to_jsonable_python(_stance("x", record="adr-x"))
    item = Item(stance_id="stance.x", title="Stance on x", path="Wiki/Stances/x.md", pages=[_listed("adr-x")], current=stance["payload"])
    draft = {key: value for key, value in _rewritten(item).items() if key != "changes"}

    with pytest.raises(ValidationError, match=r"changes\n  Field required"):
        _typed(refresh, draft)


async def test_a_finding_left_in_unchanged_evidence_reaches_the_judge_and_the_page_still_lands(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-x": _record("adr-x")}, stances={"stance.x": _stance("x", record="adr-x")})
    left = "Evidence links [[paper.gone]], which no page of the knowledge base carries"

    def leave_the_evidence_finding(item: Item) -> dict[str, object]:
        return {**_rewritten(item), "findings_left": [left]}

    scripted = Scripted(rewrite=leave_the_evidence_finding)

    state = await _run(sandbox, bases, scripted, changed="adr-x")

    reviewed: list[dict[str, object]] = to_jsonable_python(scripted.reviewed[0])
    assert [draft["findings_left"] for draft in reviewed] == [[left]], "the judge reads the finding the worker left"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}


def _two_proposals(group: Group) -> list[dict[str, object]]:
    """A placement worker proposing stance.agent-memory for its group's first page and stance.reranking for its second."""
    first, second = group["pages"]
    return [
        {
            "page_id": first["page_id"],
            "placement": "new",
            "stance_id": "stance.agent-memory",
            "title": "Agent memory",
            "reason": "Papers argue for it.",
        },
        {
            "page_id": second["page_id"],
            "placement": "new",
            "stance_id": "stance.reranking",
            "title": "Reranking",
            "reason": "Papers argue for it.",
        },
    ]


async def _kept_beside_memory(
    sandbox: AsyncMonty, findings: list[Finding], *, refused: frozenset[str] = frozenset()
) -> tuple[list[dict[str, object]], Bases, WorkflowState]:
    """What check_findings keeps when stance.reranking is backed by `findings` beside stance.agent-memory, with the bases and the run.

    stance.agent-memory is backed by BACKED. The run also rewrites stance.x, whose record changed, and read_page refuses
    the digests `refused` names.
    """
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
        refused_digests=refused,
    )

    def back(proposal: Proposal) -> list[Finding]:
        return BACKED if proposal["stance_id"] == "stance.agent-memory" else findings

    state = await _run(sandbox, bases, Scripted(place=_two_proposals, back=back), changed="adr-x,adr-1,adr-2")
    kept: list[dict[str, object]] = to_jsonable_python(state.values["check_findings"])
    return kept, bases, state


def _reranking(claims: list[str], *, sides: tuple[str, ...] = ()) -> dict[str, object]:
    """The proposal stance.reranking as the run keeps it, proposed for adr-2, each finding supporting it unless `sides` says otherwise."""
    shown = [_shown(claim, sides[index] if sides else "supports") for index, claim in enumerate(claims)]
    return {
        "stance_id": "stance.reranking",
        "title": "Reranking",
        "pages": [_listed("adr-2")],
        "findings": shown,
        "contests_only": all(finding["side"] == "contests" for finding in shown),
    }


@pytest.mark.parametrize(
    "findings",
    [
        pytest.param([_finding("paper.dense-retrieval#^c1"), _finding("art.memory-survey#^c9")], id="a-missing-anchor"),
        pytest.param([_finding("paper.dense-retrieval#^c1"), _finding("post.keep-shipping#^c1")], id="a-skipped-digest"),
        pytest.param([_finding("paper.dense-retrieval#^c1"), _finding("con.memory#^c1")], id="a-page-that-is-no-digest"),
        pytest.param([_finding("paper.dense-retrieval#^c1"), _finding("paper.dense-retrieval#^c2")], id="one-digest-cited-twice"),
        pytest.param([_finding("paper.dense-retrieval#^c1"), _finding("paper.made-up#^c1")], id="an-id-the-base-does-not-hold"),
    ],
)
async def test_a_proposal_left_with_one_digest_after_the_checks_is_dropped(sandbox: AsyncMonty, findings: list[Finding]) -> None:
    kept, _, _ = await _kept_beside_memory(sandbox, findings)

    assert kept == [MEMORY], "stance.reranking keeps findings from one digest at most and is dropped, and stance.agent-memory stays"


async def test_a_finding_that_fails_a_check_is_dropped_and_the_proposal_keeps_the_rest(sandbox: AsyncMonty) -> None:
    findings = [
        _finding("[[paper.dense-retrieval#^c2|Dense retrieval]]"),
        _finding("art.memory-survey#^c9"),
        _finding("post.keep-shipping#^c1"),
        _finding("page:research/talk.agent-loops#^c4"),
        _finding("paper.dense-retrieval#^c2"),
    ]

    kept, bases, _ = await _kept_beside_memory(sandbox, findings)

    assert kept == [MEMORY, _reranking(["paper.dense-retrieval#^c2", "talk.agent-loops#^c4"])], (
        "a claim cited as a wikilink or a collection-qualified ref counts once as its bare reference, and each failed finding goes alone"
    )
    digest_reads = [page_id for knowledge_base, page_id in bases.reads if knowledge_base == "research" and page_id in DIGESTS]
    assert sorted(digest_reads) == ["art.memory-survey", "paper.dense-retrieval", "post.keep-shipping", "talk.agent-loops"], (
        "check_findings reads each cited digest once, however many proposals cite it"
    )


async def test_a_digest_read_page_refuses_drops_its_findings_and_the_rewrite_is_still_written(sandbox: AsyncMonty) -> None:
    findings = [_finding("paper.dense-retrieval#^c2"), _finding("talk.agent-loops#^c4")]

    kept, bases, state = await _kept_beside_memory(sandbox, findings, refused=frozenset({"talk.agent-loops"}))

    assert ("research", "talk.agent-loops") in bases.reads
    assert kept == [MEMORY], "the refused digest's finding is dropped, which leaves stance.reranking one digest"
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"], "the judged rewrite lands"
    assert _outcome(state) == "succeeded"


@pytest.mark.parametrize(("sides", "contests_only"), [(("contests", "contests"), True), (("supports", "contests"), False)])
async def test_a_proposal_whose_findings_all_contest_it_is_kept_and_marked(
    sandbox: AsyncMonty, sides: tuple[str, str], *, contests_only: bool
) -> None:
    findings = [_finding("talk.agent-loops#^c4", sides[0]), _finding("art.memory-survey#^c3", sides[1])]

    kept, _, state = await _kept_beside_memory(sandbox, findings)

    reranking = _reranking(["talk.agent-loops#^c4", "art.memory-survey#^c3"], sides=sides)
    assert reranking["contests_only"] is contests_only
    assert kept == [MEMORY, reranking], "a proposal the literature argues against is kept"
    assert _value(state, "write_stances")["proposed"] == [MEMORY, reranking], "and reaches the card marked as such"


async def test_a_proposals_judge_rejection_backs_the_proposals_again_and_never_reruns_the_rewrites(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )
    first = [_finding("paper.dense-retrieval#^c2"), _finding("art.memory-survey#^c3")]

    def back(_: Proposal) -> list[Finding]:
        return first if not scripted.judged else BACKED

    feedback = "stance.agent-memory: paper.dense-retrieval#^c2 is about training data and does not bear on agent memory."
    scripted = Scripted(
        place=_proposing("stance.agent-memory", "Agent memory"), back=back, proposal_verdicts=[{"outcome": "reject", "feedback": feedback}]
    )

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    rejected = _kept("stance.agent-memory", "Agent memory", [_listed("adr-1")], ["paper.dense-retrieval#^c2", "art.memory-survey#^c3"])
    assert scripted.judged == [[rejected], [MEMORY]], "the second backing answers the judge's feedback"
    assert scripted.calls.count("back_proposals") == 2
    assert (scripted.calls.count("refresh_stances"), scripted.calls.count("check_stances")) == (1, 1), "the rewrites run once"
    assert {node_id: [entry.feedback for entry in entries] for node_id, entries in state.retry_feedback.items()} == {
        "back_proposals": [feedback]
    }
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"]
    assert _value(state, "write_stances")["proposed"] == [MEMORY]


async def test_a_run_with_no_proposal_backs_nothing_and_reads_no_digest(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    assert scripted.backed == [], "no backing worker runs"
    assert scripted.calls.count("check_findings") == 1, "check_findings is code"
    assert [page_id for _, page_id in bases.reads if page_id in DIGESTS] == [], "and reads no digest"
    assert scripted.judged == [[]], "the proposals judge is reached once, on an empty list, as the rewrite judge is"
    judge = next(step for step in _spec().inline if step.name == "judge_proposals")
    assert isinstance(judge, Briefing)
    assert "When proposals is an empty list, answer outcome approve" in judge.instructions, "and approves it calling no tool"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": [], "proposed": []}
    assert _outcome(state) == "succeeded"


def test_the_backing_worker_and_the_rewrite_judge_hold_budgets_that_fit_their_work() -> None:
    steps = {step.name: step for step in _spec().inline}
    back, check = steps["back_proposal"], steps["check_stances"]
    assert isinstance(back, Briefing)
    assert isinstance(check, Briefing)
    assert set(back.tools) == {"search", "fetch", "read_page"}
    assert back.budget is not None
    assert (back.budget.requests, back.budget.tool_calls) == (20, 120)
    assert isinstance(back.output, Shape)
    finding = back.output.json_schema["properties"]["findings"]["items"]
    assert set(finding["properties"]) == {"claim", "side", "how"}, "the backing worker types no claim text"
    assert check.budget is not None
    assert (check.budget.requests, check.budget.tool_calls) == (40, 300)
