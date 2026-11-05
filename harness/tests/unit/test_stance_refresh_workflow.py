"""The shipped stance-refresh workflow, run end to end off its own files.

The graph and the steps come from the real documents under `templates/research/workflows`, never from a copy typed
out here. The five model steps are scripted and no model is reached. The seven programs run for real in the
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
the same id make one proposal, an id a listed stance carries makes no proposal, and a placement under an id no stance
carries is named on the card and the run's output. Then the findings behind a
proposal: each keeps the claim's own text copied from its digest, a missing anchor, a skipped digest, a page that is
no digest and one digest cited twice never count toward the two digests, a digest read_page refuses drops only its own
findings while the run still writes its rewrites, a claim cited as a search ref or a wikilink counts as its bare
reference, a proposal whose findings all contest it is kept and marked, a rejection by the proposals judge backs the
proposals again and never reruns the rewrites, and a run with no proposal backs nothing and reads no digest. Then the
order: the proposals start only once every rewrite is written, so a rewrite judge's rejection redrafts before the
write, and a proposals judge that spends its last attempt, one nobody answers and a backing map that aborts each fail
the run with the rewrite written. A proposal whose backing worker altered its copy is dropped after the judge, one that
only reordered its pages is kept, and a backing that took another proposal's id leaves the honest proposal on the card,
since the map collects its answers as they finish. A claim two matching backings give opposite sides is dropped
whichever finishes first. Then the stance flags: an approved proposal is written as a pending flag of kind stance that
the research schema takes, a pending one is written again and keeps its created date, one the owner decided is left
alone and listed, and a refused write or a refused listing is named while the run succeeds. Last, the budgets of the
backing worker and the rewrite judge.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

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
from core.ai.workflows.exceptions import RepeatedFailureError
from engram.kb.contracts import AuthoringPayload, KnowledgeBaseSchema, parse, render
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, ModelRetry, UsageLimitExceeded
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


class Reason(TypedDict):
    page_id: str
    reason: str


class Proposal(TypedDict):
    stance_id: str
    title: str
    pages: list[Page]
    reasons: list[Reason]
    stance_ids: list[str]


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


class FlagSection(TypedDict):
    heading: str
    body: str


class FlagPayload(TypedDict):
    frontmatter: dict[str, object]
    sections: list[FlagSection]


class FlagWriteCall(TypedDict):
    knowledge_base: str
    payload: FlagPayload
    path: NotRequired[str]


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


@dataclass(frozen=True)
class FlagFile:
    """One flag page of the scripted research base as its file holds it: status, kind, path and created date."""

    status: str = "pending"
    flag_kind: str = "stance"
    path: str = ""
    created: str = "2026-10-30"


@dataclass
class Bases:
    """The records base and the research base the programs read and write, scripted.

    `records` holds each records page's frontmatter by id, and `stances` each stance page as read_page answers it.
    `digests` holds the research pages read_page answers with claims, and `refused_digests` names the ids among them
    read_page refuses. A research id held by none of them is one read_page cannot find. A records page missing from `records` is one read_page cannot find and list_pages does not list. `outside_root`
    names records pages whose file lies in an app's own docs folder, beyond the records base's root, which list_pages
    lists and read_page refuses the way the real tool does. `unreadable` names stance ids list_pages lists and
    read_page refuses. `refusing` names the page ids write_note refuses. `flags` holds each indexed flag's file by id,
    which read_page reads, at `Wiki/Flags/<id without flag.>.md` unless its path says otherwise. list_pages lists each
    at its file's status, or at the status `indexed_status` names for an index that has not caught up with the file.
    `refused_flags` names flag ids read_page refuses, `refuses_flag_listing` makes list_pages refuse the flags, and
    `unindexed_files` names the paths read_notes finds on disk that the index does not list. `writes` and
    `flag_writes` record every write_note of a stance and of a flag, and `listed`, `reads` and `notes_read` every
    list_pages, read_page and read_notes call, in order.
    """

    records: dict[str, dict[str, str]] = field(default_factory=dict)
    stances: dict[str, dict[str, object]] = field(default_factory=dict)
    digests: dict[str, Digest] = field(default_factory=dict)
    refused_digests: frozenset[str] = frozenset()
    outside_root: frozenset[str] = frozenset()
    unreadable: frozenset[str] = frozenset()
    refusing: frozenset[str] = frozenset()
    flags: dict[str, FlagFile] = field(default_factory=dict)
    indexed_status: dict[str, str] = field(default_factory=dict)
    refused_flags: frozenset[str] = frozenset()
    refuses_flag_listing: bool = False
    unindexed_files: frozenset[str] = frozenset()
    writes: list[WriteCall] = field(default_factory=list)
    flag_writes: list[FlagWriteCall] = field(default_factory=list)
    listed: list[dict[str, object]] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)
    notes_read: list[str] = field(default_factory=list)

    async def call(self, name: str, args: dict[str, object], /) -> object:
        if name == "read_page":
            self.reads.append((str(args["knowledge_base"]), str(args["page_id"])))
            return self._read(str(args["knowledge_base"]), str(args["page_id"]))
        if name == "list_pages":
            self.listed.append(dict(args))
            return self._list(str(args["knowledge_base"]), str(args["page_type"]), str(args.get("status", "")))
        if name == "read_notes":
            path = str(args["path"])
            self.notes_read.append(path)
            return {"path": path, "exists": path in self.unindexed_files, "text": "", "threads": []}
        if name == "write_note":
            payload: FlagPayload = to_jsonable_python(args["payload"])
            page_id = str(payload["frontmatter"]["id"])
            if page_id.startswith("flag."):
                self.flag_writes.append(to_jsonable_python(args))
            else:
                self.writes.append(to_jsonable_python(args))
            if page_id in self.refusing:
                msg = "the page was refused by research's grammar:\nstatus 'settled' is not one of considering, trying, adopted, rejected"
                raise ModelRetry(msg)
            return f"{page_id} written, indexed clean"
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
        if knowledge_base == "research" and page_id in self.refused_flags:
            msg = f"{page_id} at Wiki/Flags/{page_id.removeprefix('flag.')}.md cannot be read as a payload: text before the first heading"
            raise ModelRetry(msg)
        if knowledge_base == "research" and page_id in self.flags:
            flag = self.flags[page_id]
            frontmatter = {
                "id": page_id,
                "type": "flag",
                "title": page_id,
                "status": flag.status,
                "flag_kind": flag.flag_kind,
                "about": ["[[paper.dense-retrieval]]"],
                "created": flag.created,
            }
            return {"page_id": page_id, "path": flag.path or _flag_path(page_id), "payload": {"frontmatter": frontmatter}}
        if knowledge_base == "research" and page_id in self.digests:
            digest = self.digests[page_id]
            frontmatter = {"id": page_id, "type": digest.page_type, "title": f"Title of {page_id}", "status": digest.status}
            claims = [{"anchor": anchor, "text": text, "loc": "p. 1"} for anchor, text in digest.claims.items()]
            payload = {"frontmatter": frontmatter, "sections": [{"heading": "Findings", "level": 2, "body": ""}], "claims": claims}
            return {"page_id": page_id, "path": f"Wiki/{page_id}.md", "payload": payload}
        msg = f"no indexed page {page_id!r} in knowledge base {knowledge_base!r}, list_pages and search answer the page ids it holds"
        raise ModelRetry(msg)

    def _listed_status(self, flag_id: str) -> str:
        """The status the index lists one flag at."""
        if flag_id in self.indexed_status:
            return self.indexed_status[flag_id]
        return self.flags[flag_id].status if flag_id in self.flags else "pending"

    def _list(self, knowledge_base: str, page_type: str, status: str) -> list[dict[str, str]]:
        if knowledge_base == "research" and page_type == "stance":
            return [
                {"page_id": stance_id, "page_type": "stance", "title": stance_id, "status": "considering"}
                for stance_id in [*self.stances, *sorted(self.unreadable)]
            ]
        if knowledge_base == "research" and page_type == "flag":
            if self.refuses_flag_listing:
                msg = "no page_type 'flag' in knowledge base 'research', the page_types are paper, stance"
                raise ModelRetry(msg)
            listed = [*self.flags, *sorted(self.refused_flags)]
            return [
                {"page_id": flag_id, "page_type": "flag", "title": flag_id, "status": self._listed_status(flag_id)} for flag_id in listed
            ]
        assert knowledge_base == "repo-docs", f"no program in this workflow lists the {page_type!r} pages of {knowledge_base!r}"
        return [
            {"page_id": page_id, "page_type": front["type"], "title": front["title"], "status": front["status"]}
            for page_id, front in self.records.items()
            if front["type"] == page_type and front["status"] == status
        ]


def _flag_path(flag_id: str) -> str:
    """Where a flag's file sits in the flag type's own folder."""
    return f"Wiki/Flags/{flag_id.removeprefix('flag.')}.md"


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


class CheckedFindingRow(TypedDict):
    claim: str
    claim_text: str
    side: str
    how: str


class Backing(TypedDict):
    stance_id: str
    title: str
    pages: list[Page]
    findings: list[CheckedFindingRow]
    contests_only: bool


def _judgement(backing: Backing, verdicts: list[dict[str, object]], *, duplicates: str | None = None) -> dict[str, object]:
    """A proposal judge's answer on `backing`: its id, title and pages copied, `verdicts`, and the stance it repeats."""
    judgement: dict[str, object] = {
        "stance_id": backing["stance_id"],
        "title": backing["title"],
        "pages": backing["pages"],
        "findings": verdicts,
    }
    if duplicates is not None:
        judgement["duplicates"] = duplicates
    return judgement


def _every_finding_bears(backing: Backing) -> dict[str, object]:
    """A proposal judge that finds every finding of the backing bears and the proposal repeats no stance."""
    return _judgement(
        backing, [{"claim": finding["claim"], "bears": True, "why": "It measures the position."} for finding in backing["findings"]]
    )


def _as_handed(proposal: Proposal) -> Mapping[str, object]:
    """The stance_id, title and pages a backing worker copies into its answer, exactly as it was handed."""
    return {"stance_id": proposal["stance_id"], "title": proposal["title"], "pages": proposal["pages"]}


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


PLACED_ADR_1 = Reason(page_id="adr-1", reason="Papers argue for it.")
"""The reason a proposing placement gives for adr-1."""

NO_VERDICT = "The judge gave no verdict: its judge item failed or was skipped, or its judge changed the proposal's id, title or pages."
"""Why list_proposals drops a proposal no judgement of which matches it."""

NO_BACKING = (
    "No backing of it passed the code checks: its worker failed, changed the proposal's id, title or pages, "
    "or cited claims from fewer than two digests the base holds."
)
"""Why list_proposals drops a proposal no backing of which reached the judge."""

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

    `place` answers each group's placements, `rewrite` each item's draft and `back` each proposal's findings, beside
    the stance_id, title and pages `copy` answers for it, after waiting the seconds `delays` names for its stance_id.
    The rewrite judge answers `rewrite_verdicts` in order, approving once its list runs out. `judge` answers each
    backing's judgement, after the same wait. `calls` records every node performed, in order, and `groups`, `items`,
    `reviewed`, `backed` and `judged` what each placement worker, refresh worker, rewrite judge call, backing worker
    and proposal judge was handed.
    """

    place: Callable[[Group], list[dict[str, object]]] = _skip_all
    rewrite: Callable[[Item], dict[str, object]] = _rewritten
    back: Callable[[Proposal], list[Finding]] = _backed_by_two
    copy: Callable[[Proposal], Mapping[str, object]] = _as_handed
    delays: Mapping[str, float] = field(default_factory=dict)
    rewrite_verdicts: list[dict[str, str]] = field(default_factory=list)
    judge: Callable[[Backing], dict[str, object]] = _every_finding_bears
    calls: list[str] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    reviewed: list[list[object]] = field(default_factory=list)
    backed: list[Proposal] = field(default_factory=list)
    judged: list[Backing] = field(default_factory=list)

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
            verdict = self.rewrite_verdicts.pop(0) if self.rewrite_verdicts else {"outcome": "approve", "feedback": ""}
            return _typed(implementation, verdict)
        if node_id == "back_proposals":
            proposal: Proposal = to_jsonable_python(kwargs["proposal"])
            self.backed.append(proposal)
            await asyncio.sleep(self.delays.get(proposal["stance_id"], 0))
            return _typed(implementation, {**self.copy(proposal), "findings": self.back(proposal)})
        if node_id == "judge_proposals":
            backing: Backing = to_jsonable_python(kwargs["backing"])
            self.judged.append(backing)
            await asyncio.sleep(self.delays.get(backing["stance_id"], 0))
            return _typed(implementation, self.judge(backing))
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


async def _run(
    sandbox: AsyncMonty, bases: Bases, scripted: Scripted, *, changed: str, progress: str = PROGRESS, raises: type[Exception] | None = None
) -> WorkflowState:
    """One whole run of the shipped workflow, as the schedule starts it with `changed` as its changed page ids.

    With `raises` the run must end with that error.
    """
    params = {"collection": "research", "records": "repo-docs", "progress": progress, "changed_page_ids": changed}
    state = WorkflowState(params=dict(params))
    graph = build_graph(_spec(), await _registry(sandbox, bases), approval=NoPerson(), executor=scripted)
    if raises is None:
        await graph.run(state=state)
    else:
        with pytest.raises(raises):
            await graph.run(state=state)
    return state


def _value(state: WorkflowState, node_id: str) -> dict[str, object]:
    """One node's recorded value as plain data."""
    value = to_jsonable_python(state.values[node_id])
    assert isinstance(value, dict)
    return value


def _proposals(state: WorkflowState) -> list[dict[str, object]]:
    """The proposals the run lists on its card, from list_proposals, as plain data without their reasons and refusals."""
    listed: list[dict[str, object]] = to_jsonable_python(_value(state, "list_proposals")["proposals"])
    return [{key: value for key, value in proposal.items() if key not in {"reasons", "refused"}} for proposal in listed]


def _refused(state: WorkflowState) -> dict[str, object]:
    """Each listed proposal's findings the judge refused, by stance id."""
    listed: list[dict[str, object]] = to_jsonable_python(_value(state, "list_proposals")["proposals"])
    return {str(proposal["stance_id"]): proposal["refused"] for proposal in listed}


def _dropped(state: WorkflowState) -> list[dict[str, object]]:
    """The proposals the run dropped, from list_proposals, each with its reasons."""
    dropped: list[dict[str, object]] = to_jsonable_python(_value(state, "list_proposals")["dropped"])
    return dropped


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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


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
    assert scripted.backed == [
        Proposal(
            stance_id="stance.agent-memory", title="Agent memory", pages=[_listed("adr-1")], reasons=[PLACED_ADR_1], stance_ids=["stance.x"]
        )
    ], "the backing worker is handed the reason placement gave for each page"
    assert bases.writes == []
    assert _value(state, "write_stances") == {"written": [], "failed": []}
    assert _proposals(state) == [MEMORY]


async def test_a_proposal_only_run_carries_its_judged_proposals_as_an_artifact_and_ends_noop(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert scripted.judged == [MEMORY], "the proposal judge reads every finding with the claim's own text"
    assert to_jsonable_python(state.values["judge_proposals"]) == [
        {
            "stance_id": "stance.agent-memory",
            "title": "Agent memory",
            "pages": [_listed("adr-1")],
            "findings": [
                {"claim": "paper.dense-retrieval#^c1", "bears": True, "why": "It measures the position."},
                {"claim": "art.memory-survey#^c3", "bears": True, "why": "It measures the position."},
            ],
            "duplicates": None,
        }
    ], "each judgement names its proposal and answers every finding by its claim"
    outcome = _checked(state)
    assert outcome.status == "noop", "no stance was rewritten"
    listed = [{**MEMORY, "reasons": [PLACED_ADR_1], "refused": []}]
    assert _value(state, "list_proposals") == {"proposals": listed, "dropped": []}
    assert [(ref.id, ref.node_id, ref.media_type, ref.value_digest) for ref in outcome.artifacts] == [
        ("proposed_stances", "list_proposals", "application/json", reviewed_value_digest(canonical_reviewed_value(listed)))
    ], "the card shows each approved proposal with its title, its pages and every finding's claim, text, side and sentence"


async def test_proposals_of_the_same_id_combine_into_one_with_every_page(sandbox: AsyncMonty) -> None:
    records = {f"adr-{number}": _record(f"adr-{number}") for number in range(1, 7)}
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory", first_only=True))

    state = await _run(sandbox, bases, scripted, changed=",".join(records))

    assert sorted(len(group["pages"]) for group in scripted.groups) == [1, 5], "the six pages go out in groups of five"
    assert scripted.items == []
    assert [proposal["pages"] for proposal in scripted.backed] == [[_listed("adr-1"), _listed("adr-6")]], "one worker backs the proposal"
    assert _proposals(state) == [{**MEMORY, "pages": [_listed("adr-1"), _listed("adr-6")]}]


async def test_write_stances_writes_each_rewrite_and_never_a_page_for_a_proposal(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )
    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"))

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    assert [item["stance_id"] for item in scripted.items] == ["stance.x"]
    assert [(write["path"], write["payload"]["frontmatter"]["id"]) for write in bases.writes] == [("Wiki/Stances/x.md", "stance.x")]
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
    assert _proposals(state) == [MEMORY]
    assert scripted.calls.index("write_stances") < scripted.calls.index("merge_proposals"), "the proposals wait on every rewrite"


async def test_a_proposal_under_a_stance_the_run_read_joins_that_stance_rewrite(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")})
    scripted = Scripted(place=_proposing("stance.x", "Stance on x"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert [(item["stance_id"], item["pages"]) for item in scripted.items] == [("stance.x", [_listed("adr-1")])]
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


async def test_a_proposal_under_a_stance_the_run_could_not_read_is_neither_refreshed_nor_proposed(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, unreadable=frozenset({"stance.broken"})
    )
    scripted = Scripted(place=_proposing("stance.broken", "Broken"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert scripted.items == []
    assert bases.writes == []
    assert _value(state, "write_stances") == {"written": [], "failed": []}


def _placing_under(stance_id: str, title: str) -> Callable[[Group], list[dict[str, object]]]:
    """A placement worker placing every page of its group under the existing stance it names as `stance_id`."""

    def place(group: Group) -> list[dict[str, object]]:
        return [
            {"page_id": page["page_id"], "placement": "stance", "stance_id": stance_id, "title": title, "reason": "It moves the stance."}
            for page in group["pages"]
        ]

    return place


async def test_a_placement_under_a_stance_the_base_does_not_list_is_named_on_the_output(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")})
    scripted = Scripted(place=_placing_under("stance.xx", "Stance on x"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    named = {"page_id": "adr-1", "stance_id": "stance.xx", "title": "Stance on x", "reason": "It moves the stance."}
    assert scripted.items == [], "no stance is rewritten from it"
    assert _proposals(state) == [], "and it proposes nothing"
    assert to_jsonable_python(state.values["list_unplaced"]) == [named], "the card shows it among what the run read"
    assert _value(state, "write_stance_flags")["unplaced"] == [named], "and the run's output names it"


async def test_a_placement_under_a_listed_stance_is_named_nowhere(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")})
    scripted = Scripted(place=_placing_under("stance.x", "Stance on x"))

    state = await _run(sandbox, bases, scripted, changed="adr-1")

    assert [item["stance_id"] for item in scripted.items] == ["stance.x"]
    assert to_jsonable_python(state.values["list_unplaced"]) == []
    assert _value(state, "write_stance_flags")["unplaced"] == []


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
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


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
        _finding("paper.dense-retrieval#^c2"),
        _finding("art.memory-survey#^c9"),
        _finding("post.keep-shipping#^c1"),
        _finding("talk.agent-loops#^c4"),
        _finding("paper.dense-retrieval#^c2"),
    ]

    kept, bases, _ = await _kept_beside_memory(sandbox, findings)

    assert kept == [MEMORY, _reranking(["paper.dense-retrieval#^c2", "talk.agent-loops#^c4"])], (
        "each failed finding goes alone, and a claim cited twice counts once"
    )
    digest_reads = [page_id for knowledge_base, page_id in bases.reads if knowledge_base == "research" and page_id in DIGESTS]
    assert sorted(digest_reads) == ["art.memory-survey", "paper.dense-retrieval", "post.keep-shipping", "talk.agent-loops"], (
        "check_findings reads each cited digest once, however many proposals cite it"
    )


@pytest.mark.parametrize(
    "cited",
    [
        pytest.param("unit:research/talk.agent-loops#c4", id="a-unit-ref-search-returns"),
        pytest.param("page:research/talk.agent-loops#^c4", id="a-collection-qualified-ref"),
        pytest.param("[[talk.agent-loops#^c4|Agent loops]]", id="a-wikilink"),
        pytest.param("talk.agent-loops#c4", id="an-anchor-without-its-caret"),
    ],
)
async def test_a_claim_cited_in_another_form_counts_as_its_bare_reference(sandbox: AsyncMonty, cited: str) -> None:
    kept, _, _ = await _kept_beside_memory(sandbox, [_finding("paper.dense-retrieval#^c2"), _finding(cited)])

    assert kept == [MEMORY, _reranking(["paper.dense-retrieval#^c2", "talk.agent-loops#^c4"])]


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
    assert _proposals(state) == [MEMORY, reranking], "and reaches the card marked as such"


async def test_a_run_with_no_proposal_backs_nothing_and_reads_no_digest(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )
    scripted = Scripted()

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    assert scripted.backed == [], "no backing worker runs"
    assert scripted.calls.count("check_findings") == 1, "check_findings is code"
    assert [page_id for _, page_id in bases.reads if page_id in DIGESTS] == [], "and reads no digest"
    assert (scripted.judged, scripted.calls.count("judge_proposals")) == ([], 0), (
        "no proposal judge runs, so the branch makes no model call"
    )
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
    assert _proposals(state) == []
    assert _outcome(state) == "succeeded"


def test_the_backing_worker_and_the_rewrite_judge_hold_budgets_that_fit_their_work() -> None:
    steps = {step.name: step for step in _spec().inline}
    back, check = steps["back_proposal"], steps["check_stances"]
    assert isinstance(back, Briefing)
    assert isinstance(check, Briefing)
    assert set(back.tools) == {"search", "fetch", "read_page"}
    assert "reviewer" not in back.instructions, "no reviewer sends a backing back"
    assert "stance_ids" not in back.instructions, "the backing worker is not told about the listed stance ids"
    assert "judged once, alone" in back.instructions
    judge = steps["judge_proposal"]
    assert isinstance(judge, Briefing)
    assert set(judge.tools) == {"read_page", "search"}, "the proposal judge reads and writes nothing"
    assert "a stance id you read with read_page" in judge.instructions
    assert "is ignored" in judge.instructions, "the judge is told a changed copy is ignored"
    assert back.budget is not None
    assert (back.budget.requests, back.budget.tool_calls) == (20, 120)
    assert isinstance(back.output, Shape)
    finding = back.output.json_schema["properties"]["findings"]["items"]
    assert set(finding["properties"]) == {"claim", "side", "how"}, "the backing worker types no claim text"
    assert check.budget is not None
    assert (check.budget.requests, check.budget.tool_calls) == (40, 300)


def _rewrite_and_propose() -> Bases:
    """A records base where adr-x changes stance.x and adr-1 is placed as a new stance, with the research base's digests."""
    return Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS
    )


def _three_proposals(group: Group) -> list[dict[str, object]]:
    """A placement worker proposing one new stance for each page of its group."""
    return [
        {
            "page_id": page["page_id"],
            "placement": "new",
            "stance_id": f"stance.topic-{index}",
            "title": f"Topic {index}",
            "reason": "Papers.",
        }
        for index, page in enumerate(group["pages"])
    ]


async def test_three_backing_workers_failing_alike_fail_the_run_after_the_rewrites_are_written(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2"), "adr-3": _record("adr-3")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )

    def back(_: Proposal) -> list[Finding]:
        msg = "The next request would exceed the request_limit of 20"
        raise UsageLimitExceeded(msg)

    scripted = Scripted(place=_three_proposals, back=back)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2,adr-3", raises=RepeatedFailureError)

    assert scripted.calls.count("back_proposals") == 3
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"], "the judged rewrite was written first"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}


async def test_three_judge_items_failing_alike_fail_the_run_after_the_rewrites_are_written(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2"), "adr-3": _record("adr-3")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )

    def judge(_: Backing) -> dict[str, object]:
        raise ModelHTTPError(status_code=401, model_name="judge", body={"error": "invalid api key"})

    scripted = Scripted(place=_three_proposals, judge=judge)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2,adr-3", raises=RepeatedFailureError)

    assert scripted.calls.count("judge_proposals") == 3
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"], "the judged rewrite was written first"
    assert _value(state, "write_stances") == {"written": ["stance.x"], "failed": []}
    assert bases.flag_writes == [], "a broken judge writes no flag and the run fails instead of succeeding empty"


@pytest.mark.parametrize(
    "altered",
    [
        pytest.param({"title": "Reranking beats dense retrieval alone"}, id="an-altered-title"),
        pytest.param({"pages": [_listed("adr-1")]}, id="altered-pages"),
        pytest.param({"stance_id": "stance.dense-reranking"}, id="an-altered-stance-id"),
    ],
)
async def test_a_proposal_its_worker_copied_back_altered_is_dropped_after_the_judge(
    sandbox: AsyncMonty, altered: dict[str, object]
) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )

    def copy(proposal: Proposal) -> Mapping[str, object]:
        return {**_as_handed(proposal), **altered} if proposal["stance_id"] == "stance.reranking" else _as_handed(proposal)

    def back(proposal: Proposal) -> list[Finding]:
        return (
            BACKED
            if proposal["stance_id"] == "stance.agent-memory"
            else [_finding("paper.dense-retrieval#^c1"), _finding("talk.agent-loops#^c4")]
        )

    scripted = Scripted(place=_two_proposals, back=back, copy=copy)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    judged = sorted(backing["stance_id"] for backing in scripted.judged)
    assert judged == sorted(["stance.agent-memory", str(altered.get("stance_id", "stance.reranking"))]), "the judge reads the copy"
    assert _proposals(state) == [MEMORY], "the altered copy is dropped and the honest proposal passes"
    assert [(proposal["stance_id"], proposal["reasons"]) for proposal in _dropped(state)] == [("stance.reranking", [NO_BACKING])]


async def test_a_rewrite_judge_rejection_redrafts_once_and_the_proposals_still_wait_for_the_write(sandbox: AsyncMonty) -> None:
    bases = _rewrite_and_propose()

    def rewrite(item: Item) -> dict[str, object]:
        return _rewritten(item, changing="Position" if len(scripted.items) == 1 else "Why")

    feedback = "stance.x: the Position names no records page."
    scripted = Scripted(
        place=_proposing("stance.agent-memory", "Agent memory"),
        rewrite=rewrite,
        rewrite_verdicts=[{"outcome": "reject", "feedback": feedback}],
    )

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1")

    assert (scripted.calls.count("refresh_stances"), scripted.calls.count("check_stances")) == (2, 2)
    assert scripted.calls.count("write_stances") == 1, "the rewrite is written once, after the second judgement"
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"]
    bodies = {section["heading"]: section["body"] for section in bases.writes[0]["payload"]["sections"]}
    assert bodies["Why"] == "Rewritten, resting on `adr-x`.", "the approved second draft is the one written"
    assert bodies["Position"] == "Position of x, citing [[paper.a]].", "the rejected first draft is not"
    assert scripted.calls.index("write_stances") < scripted.calls.index("merge_proposals"), "the proposals still wait on the write"
    assert {node_id: [entry.feedback for entry in entries] for node_id, entries in state.retry_feedback.items()} == {
        "refresh_stances": [feedback]
    }
    assert _proposals(state) == [MEMORY]


@pytest.mark.parametrize(
    ("copied", "kept"),
    [
        pytest.param([_listed("adr-2"), _listed("adr-1")], True, id="reordered-pages"),
        pytest.param([_listed("adr-1"), _listed("adr-2"), _listed("adr-9")], False, id="an-added-page"),
        pytest.param([_listed("adr-1")], False, id="a-missing-page"),
    ],
)
async def test_a_proposal_whose_worker_reordered_its_pages_is_kept_and_one_with_a_page_added_or_missing_is_dropped(
    sandbox: AsyncMonty, copied: list[Page], *, kept: bool
) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )

    def copy(proposal: Proposal) -> Mapping[str, object]:
        return {**_as_handed(proposal), "pages": copied}

    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"), copy=copy)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    made = [_listed("adr-1"), _listed("adr-2")]
    assert [proposal["pages"] for proposal in scripted.backed] == [made]
    assert _proposals(state) == ([{**MEMORY, "pages": made}] if kept else []), "the card lists the pages as placement made them"


@pytest.mark.parametrize(
    "taken",
    [
        pytest.param({"stance_id": "stance.agent-memory"}, id="its-id-alone"),
        pytest.param(
            {"stance_id": "stance.agent-memory", "title": "Agent memory", "pages": [_listed("adr-1")]}, id="its-id-title-and-pages"
        ),
    ],
)
async def test_a_backing_that_takes_another_proposals_id_is_dropped_and_the_honest_proposal_still_reaches_the_card(
    sandbox: AsyncMonty, taken: dict[str, object]
) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )
    other = [_finding("paper.dense-retrieval#^c1"), _finding("talk.agent-loops#^c4")]

    def copy(proposal: Proposal) -> Mapping[str, object]:
        return {**_as_handed(proposal), **taken} if proposal["stance_id"] == "stance.reranking" else _as_handed(proposal)

    def back(proposal: Proposal) -> list[Finding]:
        return BACKED if proposal["stance_id"] == "stance.agent-memory" else other

    scripted = Scripted(place=_two_proposals, back=back, copy=copy, delays={"stance.agent-memory": 0.2})

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    answered: list[dict[str, object]] = to_jsonable_python(state.values["back_proposals"])
    assert [answer["findings"] for answer in answered] == [other, BACKED], "the map collects answers as they finish, not in item order"
    if "title" not in taken:
        assert _proposals(state) == [MEMORY], "the backing under the wrong title matches no proposal, and the honest one stands"
    else:
        shown = _proposals(state)
        assert [(proposal["stance_id"], proposal["title"], proposal["pages"]) for proposal in shown] == [
            ("stance.agent-memory", "Agent memory", [_listed("adr-1")])
        ]
        claims = sorted(finding["claim"] for finding in _findings_of(shown[0]))
        assert claims == ["art.memory-survey#^c3", "paper.dense-retrieval#^c1", "talk.agent-loops#^c4"], (
            "the proposal carries the honest backing's findings beside the copy's, each claim once"
        )


@pytest.mark.parametrize("first", ["stance.agent-memory", "stance.reranking"], ids=["the-honest-backing-first", "the-copy-first"])
@pytest.mark.parametrize(
    ("copied", "shown"),
    [
        pytest.param(
            [_finding("paper.dense-retrieval#^c1", "contests"), _finding("talk.agent-loops#^c4", "contests")],
            [("art.memory-survey#^c3", "contests"), ("talk.agent-loops#^c4", "contests")],
            id="the-rest-all-contest",
        ),
        pytest.param(
            [_finding("paper.dense-retrieval#^c1", "contests"), _finding("art.memory-survey#^c3", "contests")],
            [("art.memory-survey#^c3", "contests")],
            id="one-digest-left",
        ),
    ],
)
async def test_a_claim_two_matching_backings_give_opposite_sides_is_dropped_whichever_finishes_first(
    sandbox: AsyncMonty, first: str, copied: list[Finding], shown: list[tuple[str, str]]
) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )
    honest = [_finding("paper.dense-retrieval#^c1", "supports"), _finding("art.memory-survey#^c3", "contests")]

    def copy(proposal: Proposal) -> Mapping[str, object]:
        taken = {"stance_id": "stance.agent-memory", "title": "Agent memory", "pages": [_listed("adr-1")]}
        return {**_as_handed(proposal), **taken} if proposal["stance_id"] == "stance.reranking" else _as_handed(proposal)

    def back(proposal: Proposal) -> list[Finding]:
        return honest if proposal["stance_id"] == "stance.agent-memory" else copied

    later = "stance.reranking" if first == "stance.agent-memory" else "stance.agent-memory"
    scripted = Scripted(place=_two_proposals, back=back, copy=copy, delays={later: 0.2})

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    answered: list[dict[str, object]] = to_jsonable_python(state.values["back_proposals"])
    assert answered[0]["findings"] == (honest if first == "stance.agent-memory" else copied)
    listed = _proposals(state)
    if len({claim.split("#")[0] for claim, _ in shown}) < 2:
        assert listed == [], "a proposal left with findings from one digest leaves the card"
        [dropped] = [proposal for proposal in _dropped(state) if proposal["stance_id"] == "stance.agent-memory"]
        assert "paper.dense-retrieval#^c1 was given opposite sides by its backings." in to_jsonable_python(dropped["reasons"])
        return
    assert [(proposal["stance_id"], proposal["title"], proposal["pages"]) for proposal in listed] == [
        ("stance.agent-memory", "Agent memory", [_listed("adr-1")])
    ]
    assert sorted((finding["claim"], finding["side"]) for finding in _findings_of(listed[0])) == shown, (
        "the claim the backings disagree on is on the card under neither side"
    )
    assert listed[0]["contests_only"] is True, "contests_only counts only what remains"


def _findings_of(proposal: dict[str, object]) -> list[dict[str, str]]:
    """One listed proposal's findings, typed."""
    findings: list[dict[str, str]] = to_jsonable_python(proposal["findings"])
    return findings


MEMORY_FLAG = FlagPayload(
    frontmatter={
        "id": "flag.stance-agent-memory",
        "type": "flag",
        "title": "Agent memory",
        "summary": "A proposed stance with 2 findings that support it and 0 that contest it.",
        "status": "pending",
        "flag_kind": "stance",
        "about": ["[[paper.dense-retrieval]]", "[[art.memory-survey]]"],
    },
    sections=[
        FlagSection(
            heading="Why",
            body=(
                "The stance refresh proposed this stance from these records pages. They live in another knowledge base, so each "
                "is named by its id.\n\n- `adr-1` (adr, accepted): Papers argue for it."
            ),
        ),
        FlagSection(
            heading="Proposal",
            body="Accepting this flag asks for a new stance page that holds this stance: Agent memory. It is drafted by hand from the evidence below.",
        ),
        FlagSection(
            heading="Evidence", body=f"- [[paper.dense-retrieval#^c1]] supports: {HOW}\n- [[art.memory-survey#^c3]] supports: {HOW}"
        ),
    ],
)
"""The flag page stance.agent-memory is written as, when adr-1 proposes it and BACKED backs it."""


def _flag_written(bases: Bases, *, path: str = _flag_path("flag.stance-agent-memory")) -> FlagPayload:
    """The payload of the one flag write_note was handed, checked to land at `path`, the new agent-memory flag's own path by default."""
    [write] = bases.flag_writes
    assert write.get("path", "") == path, "a flag is written where its file sits, or at the path read_notes checked when new"
    return write["payload"]


def _flag_bases(
    *,
    flags: dict[str, FlagFile] | None = None,
    indexed_status: dict[str, str] | None = None,
    refused_flags: frozenset[str] = frozenset(),
    unindexed_files: frozenset[str] = frozenset(),
) -> Bases:
    """A records base where adr-1 is placed as a new stance, beside the research base's digests and these flags."""
    return Bases(
        records={"adr-1": _record("adr-1")},
        stances={"stance.x": _stance("x", record="adr-other")},
        digests=DIGESTS,
        flags=flags or {},
        indexed_status=indexed_status or {},
        refused_flags=refused_flags,
        unindexed_files=unindexed_files,
    )


async def test_an_approved_stance_proposal_is_written_as_a_pending_stance_flag(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    assert _flag_written(bases) == MEMORY_FLAG
    assert _value(state, "write_stance_flags") == {
        "written": ["flag.stance-agent-memory"],
        "kept_decided": [],
        "failed": [],
        "dropped": [],
        "unplaced": [],
    }
    assert bases.notes_read == ["Wiki/Flags/stance-agent-memory.md"], "a new flag's path is checked on disk first"
    assert bases.writes == [], "no stance page is written for a proposal"


async def test_a_pending_stance_flag_is_written_again_with_the_newer_findings_and_keeps_its_created_date(sandbox: AsyncMonty) -> None:
    bases = Bases(
        records={"adr-1": _record("adr-1")},
        stances={"stance.x": _stance("x", record="adr-other")},
        digests=DIGESTS,
        flags={"flag.stance-agent-memory": FlagFile()},
    )

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    written = _flag_written(bases, path="Wiki/Flags/stance-agent-memory.md")
    assert written["frontmatter"] == {**MEMORY_FLAG["frontmatter"], "created": "2026-10-30"}
    assert written["sections"] == MEMORY_FLAG["sections"]
    assert _value(state, "write_stance_flags") == {
        "written": ["flag.stance-agent-memory"],
        "kept_decided": [],
        "failed": [],
        "dropped": [],
        "unplaced": [],
    }


@pytest.mark.parametrize("status", ["accepted", "dismissed", "completed"])
async def test_a_stance_flag_the_owner_decided_is_never_written_again_and_is_listed(sandbox: AsyncMonty, status: str) -> None:
    bases = Bases(
        records={"adr-1": _record("adr-1")},
        stances={"stance.x": _stance("x", record="adr-other")},
        digests=DIGESTS,
        flags={"flag.stance-agent-memory": FlagFile(status=status)},
    )

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    assert bases.flag_writes == []
    assert _value(state, "write_stance_flags") == {
        "written": [],
        "kept_decided": ["flag.stance-agent-memory"],
        "failed": [],
        "dropped": [],
        "unplaced": [],
    }


@pytest.mark.parametrize(
    ("refusing", "refuses_listing", "reason"),
    [
        pytest.param(frozenset({"flag.stance-agent-memory"}), False, "write_note refused the page: ", id="a-refused-write"),
        pytest.param(frozenset(), True, "list_pages refused the flags, so no flag was written: ", id="a-refused-listing"),
    ],
)
async def test_a_refused_stance_flag_is_named_and_the_run_still_succeeds(
    sandbox: AsyncMonty, refusing: frozenset[str], reason: str, *, refuses_listing: bool
) -> None:
    bases = Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
        refusing=refusing,
        refuses_flag_listing=refuses_listing,
    )

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-x,adr-1")

    result = _value(state, "write_stance_flags")
    assert (result["written"], result["kept_decided"]) == ([], [])
    failed: list[dict[str, str]] = to_jsonable_python(result["failed"])
    assert [entry["flag_id"] for entry in failed] == ["flag.stance-agent-memory"]
    assert failed[0]["reason"].startswith(reason), "the refusal's own words reach the run card"
    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"], "the judged rewrite still lands"
    assert _outcome(state) == "succeeded"


async def test_the_research_schema_takes_a_stance_flag_as_the_run_writes_it(sandbox: AsyncMonty) -> None:
    bases = Bases(records={"adr-1": _record("adr-1")}, stances={"stance.x": _stance("x", record="adr-other")}, digests=DIGESTS)
    await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")
    schema = KnowledgeBaseSchema.from_path(RESEARCH.parent / "knowledge-base-schema.json")

    payload = AuthoringPayload.model_validate({**_flag_written(bases), "knowledge_base_schema": schema})
    note = parse(render(payload, spec=schema), source_path="Wiki/Flags/stance-agent-memory.md", spec=schema)

    assert (note.frontmatter.id, note.frontmatter.type, note.frontmatter.status) == ("flag.stance-agent-memory", "flag", "pending")


async def test_a_stance_flag_accepted_on_disk_before_the_index_caught_up_is_never_written_again(sandbox: AsyncMonty) -> None:
    bases = _flag_bases(
        flags={"flag.stance-agent-memory": FlagFile(status="accepted")}, indexed_status={"flag.stance-agent-memory": "pending"}
    )

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    assert bases.flag_writes == [], "the owner's decision in the file wins over the index"
    assert _value(state, "write_stance_flags") == {
        "written": [],
        "kept_decided": ["flag.stance-agent-memory"],
        "failed": [],
        "dropped": [],
        "unplaced": [],
    }


async def test_a_pending_stance_flag_the_owner_moved_is_written_where_it_now_sits(sandbox: AsyncMonty) -> None:
    moved = "Wiki/Flags/Done/stance-agent-memory.md"
    bases = _flag_bases(flags={"flag.stance-agent-memory": FlagFile(path=moved)})

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    assert _flag_written(bases, path=moved)["sections"] == MEMORY_FLAG["sections"]
    assert _value(state, "write_stance_flags") == {
        "written": ["flag.stance-agent-memory"],
        "kept_decided": [],
        "failed": [],
        "dropped": [],
        "unplaced": [],
    }


@pytest.mark.parametrize(
    ("bases_of", "reason"),
    [
        pytest.param(
            lambda: _flag_bases(refused_flags=frozenset({"flag.stance-agent-memory"})),
            "read_page refused the flag, so it was left as it is: ",
            id="a-flag-read-page-refuses",
        ),
        pytest.param(
            lambda: _flag_bases(flags={"flag.stance-agent-memory": FlagFile(flag_kind="idea")}),
            "a flag of kind idea already holds this id, so it was left as it is",
            id="a-flag-of-another-kind",
        ),
        pytest.param(
            lambda: _flag_bases(unindexed_files=frozenset({"Wiki/Flags/stance-agent-memory.md"})),
            "a file the index does not list sits at Wiki/Flags/stance-agent-memory.md, so it was left as it is.",
            id="a-file-the-index-does-not-list",
        ),
    ],
)
async def test_a_page_the_run_cannot_safely_replace_is_named_and_left_alone(
    sandbox: AsyncMonty, bases_of: Callable[[], Bases], reason: str
) -> None:
    bases = bases_of()

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory")), changed="adr-1")

    assert bases.flag_writes == []
    result = _value(state, "write_stance_flags")
    assert (result["written"], result["kept_decided"]) == ([], [])
    failed: list[dict[str, str]] = to_jsonable_python(result["failed"])
    assert [entry["flag_id"] for entry in failed] == ["flag.stance-agent-memory"]
    assert failed[0]["reason"].startswith(reason)
    assert _outcome(state) == "noop", "the run still ends as it would have"


@pytest.mark.parametrize(
    ("backed", "summary", "about"),
    [
        pytest.param(
            [_finding("paper.dense-retrieval#^c1", "supports"), _finding("art.memory-survey#^c3", "contests")],
            "A proposed stance with 1 finding that supports it and 1 that contests it.",
            ["[[paper.dense-retrieval]]", "[[art.memory-survey]]"],
            id="one-of-each",
        ),
        pytest.param(
            [_finding("paper.dense-retrieval#^c1", "contests"), _finding("art.memory-survey#^c3", "contests")],
            "A proposed stance the literature argues against: all 2 of its findings contest it.",
            ["[[paper.dense-retrieval]]", "[[art.memory-survey]]"],
            id="all-contest",
        ),
        pytest.param(
            [_finding("paper.dense-retrieval#^c1"), _finding("paper.dense-retrieval#^c2"), _finding("art.memory-survey#^c3")],
            "A proposed stance with 3 findings that support it and 0 that contest it.",
            ["[[paper.dense-retrieval]]", "[[art.memory-survey]]"],
            id="one-digest-cited-twice",
        ),
    ],
)
async def test_a_stance_flag_counts_its_findings_in_its_summary_and_links_each_digest_once(
    sandbox: AsyncMonty, backed: list[Finding], summary: str, about: list[str]
) -> None:
    bases = _flag_bases()

    def back(_: Proposal) -> list[Finding]:
        return backed

    await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory"), back=back), changed="adr-1")

    frontmatter = _flag_written(bases)["frontmatter"]
    assert (frontmatter["summary"], frontmatter["about"]) == (summary, about)


def _four_proposals(group: Group) -> list[dict[str, object]]:
    """A placement worker proposing one new stance per page of its group: memory, retrieval, reranking and routing."""
    topics = ["memory", "retrieval", "reranking", "routing"]
    return [
        {
            "page_id": page["page_id"],
            "placement": "new",
            "stance_id": f"stance.{topics[index]}",
            "title": topics[index].capitalize(),
            "reason": "Papers argue for it.",
        }
        for index, page in enumerate(group["pages"])
    ]


def _judge_the_four(backing: Backing) -> dict[str, object]:
    """A proposal judge that refuses one claim each of memory and retrieval, calls reranking a duplicate and fails on routing."""
    stance_id = backing["stance_id"]
    if stance_id == "stance.routing":
        msg = "The next request would exceed the request_limit of 15"
        raise UsageLimitExceeded(msg)
    refused = {"stance.memory": "talk.agent-loops#^c4", "stance.retrieval": "art.memory-survey#^c3"}.get(stance_id, "")
    verdicts = [
        {
            "claim": finding["claim"],
            "bears": finding["claim"] != refused,
            "why": "It only shares the topic." if finding["claim"] == refused else "It measures it.",
        }
        for finding in backing["findings"]
    ]
    return _judgement(backing, verdicts, duplicates="stance.x" if stance_id == "stance.reranking" else None)


async def test_each_proposal_is_judged_alone_and_only_the_one_left_with_two_bearing_digests_is_written(sandbox: AsyncMonty) -> None:
    records = {page_id: _record(page_id) for page_id in ("adr-x", "adr-1", "adr-2", "adr-3", "adr-4")}
    bases = Bases(records=records, stances={"stance.x": _stance("x", record="adr-x")}, digests=DIGESTS)

    def back(proposal: Proposal) -> list[Finding]:
        return [*BACKED, _finding("talk.agent-loops#^c4")] if proposal["stance_id"] == "stance.memory" else BACKED

    scripted = Scripted(place=_four_proposals, back=back, judge=_judge_the_four, delays={"stance.memory": 0.3, "stance.retrieval": 0.1})

    state = await _run(sandbox, bases, scripted, changed=",".join(records))

    assert [write["path"] for write in bases.writes] == ["Wiki/Stances/x.md"], "the rewrite is written"
    assert scripted.calls.count("back_proposals") == 4, "each proposal is backed once, and a weak finding is never sent back"
    assert scripted.calls.count("judge_proposals") == 4
    for program in ("merge_proposals", "check_findings", "list_proposals", "write_stance_flags"):
        assert scripted.calls.count(program) == 1, f"{program} runs once, after everything it waits on"
    [written] = bases.flag_writes
    assert written["payload"]["frontmatter"]["id"] == "flag.stance-memory"
    assert written["payload"]["sections"][2]["body"] == (
        f"- [[paper.dense-retrieval#^c1]] supports: {HOW}\n- [[art.memory-survey#^c3]] supports: {HOW}\n\n"
        "Left out:\n\n- `talk.agent-loops#^c4`: The judge says it does not bear: It only shares the topic."
    ), "the findings that bear, then each finding the judge refused with its reason"
    assert _refused(state) == {
        "stance.memory": [{"claim": "talk.agent-loops#^c4", "why": "The judge says it does not bear: It only shares the topic."}]
    }
    dropped = [
        {
            "stance_id": "stance.retrieval",
            "title": "Retrieval",
            "reasons": [
                "Fewer than two digests are left with findings that bear.",
                "art.memory-survey#^c3 does not bear: It only shares the topic.",
            ],
        },
        {"stance_id": "stance.reranking", "title": "Reranking", "reasons": ["The judge found it repeats the existing stance stance.x."]},
        {"stance_id": "stance.routing", "title": "Routing", "reasons": [NO_VERDICT]},
    ]
    assert _dropped(state) == dropped
    flags = _value(state, "write_stance_flags")
    assert (flags["written"], flags["dropped"]) == (["flag.stance-memory"], dropped), "the run's output says why each one went"
    assert _outcome(state) == "succeeded"


def _with_memory_and_reranking() -> Bases:
    """A records base where adr-x changes stance.x, adr-1 proposes stance.agent-memory and adr-2 stance.reranking."""
    return Bases(
        records={"adr-x": _record("adr-x"), "adr-1": _record("adr-1"), "adr-2": _record("adr-2")},
        stances={"stance.x": _stance("x", record="adr-x")},
        digests=DIGESTS,
    )


def _takes_memory_id(proposal: Proposal) -> Mapping[str, object]:
    """A backing worker for stance.reranking that answers under stance.agent-memory's id with its own title and pages."""
    copied = _as_handed(proposal)
    return {**copied, "stance_id": "stance.agent-memory"} if proposal["stance_id"] == "stance.reranking" else copied


def _memory_or_other(proposal: Proposal) -> list[Finding]:
    """BACKED for stance.agent-memory, and two other findings for every other proposal."""
    return (
        BACKED
        if proposal["stance_id"] == "stance.agent-memory"
        else [_finding("paper.dense-retrieval#^c1"), _finding("talk.agent-loops#^c4")]
    )


def _the_copy(backing: Backing) -> bool:
    """Whether `backing` is the reranking worker's answer under stance.agent-memory's id."""
    return backing["stance_id"] == "stance.agent-memory" and backing["title"] == "Reranking"


@pytest.mark.parametrize("copy_says", ["refuses-c1", "names-a-duplicate"])
async def test_a_judgement_of_a_copy_under_another_title_never_touches_the_honest_proposal(sandbox: AsyncMonty, copy_says: str) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        if not _the_copy(backing):
            return _every_finding_bears(backing)
        verdicts = [
            {
                "claim": finding["claim"],
                "bears": copy_says != "refuses-c1" or not finding["claim"].endswith("#^c1"),
                "why": "Off topic for reranking.",
            }
            for finding in backing["findings"]
        ]
        return _judgement(backing, verdicts, duplicates="stance.x" if copy_says == "names-a-duplicate" else None)

    bases = _with_memory_and_reranking()
    scripted = Scripted(place=_two_proposals, back=_memory_or_other, copy=_takes_memory_id, judge=judge)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    assert _proposals(state) == [MEMORY], "the copy's judgement matches no proposal, so the honest one stands"
    assert _refused(state) == {"stance.agent-memory": []}


async def test_an_honest_proposal_whose_own_judge_failed_is_dropped_even_when_a_copy_was_judged(sandbox: AsyncMonty) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        if not _the_copy(backing):
            msg = "The next request would exceed the request_limit of 15"
            raise UsageLimitExceeded(msg)
        return _every_finding_bears(backing)

    bases = _with_memory_and_reranking()
    scripted = Scripted(place=_two_proposals, back=_memory_or_other, copy=_takes_memory_id, judge=judge)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    assert _proposals(state) == []
    assert [(proposal["stance_id"], proposal["reasons"]) for proposal in _dropped(state)] == [
        ("stance.agent-memory", [NO_VERDICT]),
        ("stance.reranking", [NO_BACKING]),
    ]


@pytest.mark.parametrize(
    ("duplicates", "kept"),
    [
        pytest.param("stance.x", False, id="a-stance-the-base-lists"),
        pytest.param("stance.made-up", True, id="an-id-the-base-does-not-list"),
        pytest.param("stance.agent-memory", True, id="the-proposal-s-own-id"),
    ],
)
async def test_a_proposal_is_dropped_as_a_duplicate_only_of_a_stance_the_base_lists(
    sandbox: AsyncMonty, duplicates: str, *, kept: bool
) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        verdicts = [{"claim": finding["claim"], "bears": True, "why": "It measures it."} for finding in backing["findings"]]
        return _judgement(backing, verdicts, duplicates=duplicates)

    bases = _flag_bases()

    state = await _run(sandbox, bases, Scripted(place=_proposing("stance.agent-memory", "Agent memory"), judge=judge), changed="adr-1")

    assert _proposals(state) == ([MEMORY] if kept else [])
    expected = (
        []
        if kept
        else [
            {
                "stance_id": "stance.agent-memory",
                "title": "Agent memory",
                "reasons": ["The judge found it repeats the existing stance stance.x."],
            }
        ]
    )
    assert _dropped(state) == expected


@pytest.mark.parametrize(
    "form",
    [
        pytest.param("[[{claim}]]", id="a-wikilink"),
        pytest.param("[[{claim}|label]]", id="a-labelled-wikilink"),
        pytest.param("{bare}", id="an-anchor-without-its-caret"),
        pytest.param("page:research/{claim}", id="a-collection-qualified-ref"),
        pytest.param("unit:research/{bare}", id="a-unit-ref"),
    ],
)
async def test_a_judge_s_claim_in_another_form_counts_as_its_bare_reference(sandbox: AsyncMonty, form: str) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        verdicts = [
            {
                "claim": form.format(claim=finding["claim"], bare=finding["claim"].replace("#^", "#")),
                "bears": True,
                "why": "It measures it.",
            }
            for finding in backing["findings"]
        ]
        return _judgement(backing, verdicts)

    state = await _run(
        sandbox, _flag_bases(), Scripted(place=_proposing("stance.agent-memory", "Agent memory"), judge=judge), changed="adr-1"
    )

    assert _proposals(state) == [MEMORY]


async def test_a_claim_is_kept_only_when_every_judge_that_answered_it_says_it_bears(sandbox: AsyncMonty) -> None:
    def copy(proposal: Proposal) -> Mapping[str, object]:
        copied = _as_handed(proposal)
        taken = {"stance_id": "stance.agent-memory", "title": "Agent memory", "pages": [_listed("adr-1")]}
        return {**copied, **taken} if proposal["stance_id"] == "stance.reranking" else copied

    def judge(backing: Backing) -> dict[str, object]:
        second = [finding["claim"] for finding in backing["findings"]] == ["paper.dense-retrieval#^c1", "talk.agent-loops#^c4"]
        verdicts = [
            {"claim": finding["claim"], "bears": not (second and finding["claim"].endswith("#^c1")), "why": "It answers another question."}
            for finding in backing["findings"]
        ]
        return _judgement(backing, verdicts)

    bases = _with_memory_and_reranking()
    scripted = Scripted(place=_two_proposals, back=_memory_or_other, copy=copy, judge=judge)

    state = await _run(sandbox, bases, scripted, changed="adr-x,adr-1,adr-2")

    [listed] = _proposals(state)
    assert sorted(finding["claim"] for finding in _findings_of(listed)) == ["art.memory-survey#^c3", "talk.agent-loops#^c4"], (
        "one judge saying a shared claim bears does not outvote the other"
    )
    assert _refused(state) == {
        "stance.agent-memory": [
            {"claim": "paper.dense-retrieval#^c1", "why": "The judge says it does not bear: It answers another question."}
        ]
    }


async def test_a_judge_s_verdicts_are_matched_to_findings_by_claim_and_never_by_position(sandbox: AsyncMonty) -> None:
    def back(_: Proposal) -> list[Finding]:
        return [*BACKED, _finding("talk.agent-loops#^c4")]

    def judge(backing: Backing) -> dict[str, object]:
        verdicts: list[dict[str, object]] = [
            {"claim": finding["claim"], "bears": not finding["claim"].startswith("talk."), "why": "It only shares the topic."}
            for finding in backing["findings"]
        ]
        return _judgement(backing, list(reversed(verdicts)))

    scripted = Scripted(place=_proposing("stance.agent-memory", "Agent memory"), back=back, judge=judge)

    state = await _run(sandbox, _flag_bases(), scripted, changed="adr-1")

    assert _proposals(state) == [MEMORY], "the verdicts arrive in reverse order and each still reaches its own claim"
    assert _refused(state) == {
        "stance.agent-memory": [{"claim": "talk.agent-loops#^c4", "why": "The judge says it does not bear: It only shares the topic."}]
    }


async def test_a_claim_the_judge_leaves_out_has_no_verdict_and_is_dropped(sandbox: AsyncMonty) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        first = backing["findings"][0]
        return _judgement(backing, [{"claim": first["claim"], "bears": True, "why": "It measures it."}])

    state = await _run(
        sandbox, _flag_bases(), Scripted(place=_proposing("stance.agent-memory", "Agent memory"), judge=judge), changed="adr-1"
    )

    assert _proposals(state) == []
    reasons = ["Fewer than two digests are left with findings that bear.", "art.memory-survey#^c3 has no verdict from the judge."]
    assert _dropped(state) == [{"stance_id": "stance.agent-memory", "title": "Agent memory", "reasons": reasons}]


async def test_a_judge_that_rewords_its_copy_matches_nothing_and_the_reason_says_so(sandbox: AsyncMonty) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        return {**_every_finding_bears(backing), "title": "Agent Memory"}

    state = await _run(
        sandbox, _flag_bases(), Scripted(place=_proposing("stance.agent-memory", "Agent memory"), judge=judge), changed="adr-1"
    )

    assert _proposals(state) == []
    assert _dropped(state) == [{"stance_id": "stance.agent-memory", "title": "Agent memory", "reasons": [NO_VERDICT]}]


@pytest.mark.parametrize("named", ["[[stance.x]]", "[[stance.x|X]]", "stance.x ", " [[ stance.x ]] "])
async def test_a_duplicate_named_as_a_link_or_with_spaces_counts_as_its_bare_id(sandbox: AsyncMonty, named: str) -> None:
    def judge(backing: Backing) -> dict[str, object]:
        verdicts = [{"claim": finding["claim"], "bears": True, "why": "It measures it."} for finding in backing["findings"]]
        return _judgement(backing, verdicts, duplicates=named)

    state = await _run(
        sandbox, _flag_bases(), Scripted(place=_proposing("stance.agent-memory", "Agent memory"), judge=judge), changed="adr-1"
    )

    assert _proposals(state) == []
    reasons = ["The judge found it repeats the existing stance stance.x."]
    assert _dropped(state) == [{"stance_id": "stance.agent-memory", "title": "Agent memory", "reasons": reasons}]


@pytest.mark.parametrize("copy_says", ["refuses-c1", "names-a-duplicate"])
async def test_a_copy_with_another_proposals_id_and_title_but_its_own_pages_never_touches_the_honest_proposal(
    sandbox: AsyncMonty, copy_says: str
) -> None:
    def copy(proposal: Proposal) -> Mapping[str, object]:
        copied = _as_handed(proposal)
        return (
            {**copied, "stance_id": "stance.agent-memory", "title": "Agent memory"}
            if proposal["stance_id"] == "stance.reranking"
            else copied
        )

    def judge(backing: Backing) -> dict[str, object]:
        if backing["pages"] != [_listed("adr-2")]:
            return _every_finding_bears(backing)
        verdicts = [
            {"claim": finding["claim"], "bears": copy_says != "refuses-c1" or not finding["claim"].endswith("#^c1"), "why": "Off topic."}
            for finding in backing["findings"]
        ]
        return _judgement(backing, verdicts, duplicates="stance.x" if copy_says == "names-a-duplicate" else None)

    scripted = Scripted(place=_two_proposals, back=_memory_or_other, copy=copy, judge=judge)

    state = await _run(sandbox, _with_memory_and_reranking(), scripted, changed="adr-x,adr-1,adr-2")

    assert _proposals(state) == [MEMORY], "a judgement whose page set differs counts for no proposal"
    assert _refused(state) == {"stance.agent-memory": []}


async def test_every_claim_a_kept_proposal_left_out_is_named_with_its_own_reason(sandbox: AsyncMonty) -> None:
    def copy(proposal: Proposal) -> Mapping[str, object]:
        copied = _as_handed(proposal)
        taken = {"stance_id": "stance.agent-memory", "title": "Agent memory", "pages": [_listed("adr-1")]}
        return {**copied, **taken} if proposal["stance_id"] == "stance.reranking" else copied

    def back(proposal: Proposal) -> list[Finding]:
        if proposal["stance_id"] == "stance.agent-memory":
            return [*BACKED, _finding("talk.agent-loops#^c4"), _finding("paper.dense-retrieval#^c2")]
        return [_finding("talk.agent-loops#^c4", "contests"), _finding("art.memory-survey#^c3")]

    def judge(backing: Backing) -> dict[str, object]:
        verdicts = [
            {"claim": finding["claim"], "bears": True, "why": "It measures it."}
            for finding in backing["findings"]
            if not finding["claim"].endswith("#^c2")
        ]
        return _judgement(backing, verdicts)

    bases = _with_memory_and_reranking()

    state = await _run(sandbox, bases, Scripted(place=_two_proposals, back=back, copy=copy, judge=judge), changed="adr-x,adr-1,adr-2")

    [listed] = _proposals(state)
    assert sorted(finding["claim"] for finding in _findings_of(listed)) == ["art.memory-survey#^c3", "paper.dense-retrieval#^c1"]
    refused: list[dict[str, str]] = to_jsonable_python(_refused(state)["stance.agent-memory"])
    assert sorted((entry["claim"], entry["why"]) for entry in refused) == [
        ("paper.dense-retrieval#^c2", "The judge gave it no verdict."),
        ("talk.agent-loops#^c4", "Its backings gave it opposite sides."),
    ]
    [flag] = bases.flag_writes
    evidence = flag["payload"]["sections"][2]["body"]
    assert "- `paper.dense-retrieval#^c2`: The judge gave it no verdict." in evidence
    assert "- `talk.agent-loops#^c4`: Its backings gave it opposite sides." in evidence
    assert "[[talk.agent-loops#^c4]]" not in evidence, "a claim left out is never written as a link"
