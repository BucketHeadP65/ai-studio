"""The shipped steps: what they may ask for, that every shipped workflow validates, and that every program runs.

Steps come from `harness/workflows/steps` and every template's `workflows/steps`. Workflows come from
`harness/workflows/workflows` and every template's `workflows/workflows`. The grant surface is derived from what
this app actually offers a declared step, never typed out here: a hand-written tuple can promise a tool the
app does not mount, and `harness save` then refuses the whole save at its first write.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.workflows.contracts import (
    Briefing,
    FromParameter,
    GateDecision,
    InlineStep,
    MapNode,
    ParameterSpec,
    Program,
    Shape,
    StepRegistry,
    Workflow,
    load_spec_document,
    load_step_document,
    prepare,
    step_page_id,
    validate,
)
from engram.authoring.contracts import NoteDraft
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS
from harness.features.selfauthoring.skills_plugin import registry_skill_names
from tests.support.grants import app_grants, app_reads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Mapping

    from core.ai.workflows.contracts import CodeRunner, StepDocument

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
RESEARCH_WORKFLOWS = TEMPLATES / "research" / "workflows"
REPO_DOCS_WORKFLOWS = TEMPLATES / "repo-docs" / "workflows"
BENCHMARK_WORKFLOWS = TEMPLATES / "benchmark-dataset" / "workflows"
NEWS_WORKFLOWS = TEMPLATES / "news-benchmark" / "workflows"

WORKFLOWS = (
    "research-digest",
    "research-compound",
    "research-decision-brief",
    "docs-notes-to-analysis",
    "docs-promote-initiative",
    "source-remove",
    "benchmark-digest",
    "news-digest",
    "news-link",
    "news-relate",
)

_WORKFLOW_DIR = {
    "research-digest": RESEARCH_WORKFLOWS,
    "research-compound": RESEARCH_WORKFLOWS,
    "research-decision-brief": RESEARCH_WORKFLOWS,
    "docs-notes-to-analysis": REPO_DOCS_WORKFLOWS,
    "docs-promote-initiative": REPO_DOCS_WORKFLOWS,
    "source-remove": WORKFLOW_FILES,
    "benchmark-digest": BENCHMARK_WORKFLOWS,
    "news-digest": NEWS_WORKFLOWS,
    "news-link": NEWS_WORKFLOWS,
    "news-relate": NEWS_WORKFLOWS,
}
"""Where each shipped workflow's own directory lives: its template, or `harness/workflows` for the one that stays."""


def _step_documents() -> list[StepDocument]:
    step_dirs = (WORKFLOW_FILES / "steps", *sorted(TEMPLATES.glob("*/workflows/steps")))
    return [load_step_document(path.read_text(encoding="utf-8")) for step_dir in step_dirs for path in sorted(step_dir.glob("*.md"))]


def _steps() -> dict[str, InlineStep]:
    return {doc.step.name: doc.step for doc in _step_documents()}


def _steps_by_id() -> dict[str, InlineStep]:
    """Every shipped step under the id the library mints for it, its name and its declared version together."""
    return {step_page_id(doc.step.name, doc.version): doc.step for doc in _step_documents()}


def _copied(uses: Collection[str], steps_by_id: Mapping[str, InlineStep], *, where: str) -> tuple[InlineStep, ...]:
    """The steps a `uses` list names, resolved by whole id rather than by name alone.

    The version is part of the id. Resolving by name would hand back whatever version of that name is
    on disk, so a file naming a version nothing declares would validate here and still be refused by a
    real import, which fetches the exact id out of the library.

    Raises:
        AssertionError: If any id names no shipped step file, naming every one that does not resolve.
    """
    missing = [step_id for step_id in uses if step_id not in steps_by_id]
    if missing:
        msg = f"{where} uses {', '.join(missing)}, which no shipped step file declares. On file: {', '.join(sorted(steps_by_id))}"
        raise AssertionError(msg)
    return tuple(steps_by_id[step_id] for step_id in uses)


def _uses_of(text: str) -> list[str]:
    front = text.split("---", 2)[1]
    return list(yaml.safe_load(front).get("uses", []))


def _shipped(name: str, steps_by_id: Mapping[str, InlineStep]) -> Workflow:
    """One shipped workflow with the steps its `uses` names copied in, exactly as `import_path` copies them."""
    text = (_WORKFLOW_DIR[name] / "workflows" / f"{name}.md").read_text(encoding="utf-8")
    spec = load_spec_document(text).workflow
    copied = _copied(_uses_of(text), steps_by_id, where=name)
    return spec.model_copy(update={"inline": (*spec.inline, *copied)})


def _shelf_and_workflow_glue(steps: dict[str, InlineStep]) -> dict[str, InlineStep]:
    """Every reusable step, plus the workflow-local glue tasks a reusable step's `like` points at.

    `excise_source`, `check_excision` and `removal_report` type against `plan_items` and
    `remove_artifacts`, both defined only inside `source-remove.md`'s own `tasks:`. `repair_digest` types
    against `list_repairs`, defined only inside `research-compound.md`'s own `inline:` beside `count_owner_repairs`.
    This is glue those workflows bring, not library files, so `_steps()` never sees it. Bringing it in here
    lets every reusable step build on its own, the same guarantee `_bring_every_step` gives every other file.
    """
    glue: dict[str, InlineStep] = {}
    for path in (WORKFLOW_FILES / "workflows" / "source-remove.md", RESEARCH_WORKFLOWS / "workflows" / "research-compound.md"):
        text = path.read_text(encoding="utf-8")
        glue.update({task.name: task for task in load_spec_document(text).workflow.inline})
    return {**steps, **glue}


def _bring_every_step(steps: dict[str, InlineStep]) -> Workflow:
    """Every step file in `workflows/steps/`, brought and built, with no node graph over them.

    `registry.including` builds every task a spec brings whether or not a node names it, so this
    gets a runnable implementation for a step file no shipped workflow's `uses` references yet --
    unlike `_shipped`, which only builds what a workflow actually wired.
    """
    return Workflow(
        name="every-shipped-step",
        version=1,
        description="Every shipped step file, brought and built, so each one can be run directly.",
        parameters={
            "unused": ParameterSpec(
                description="never read; `output` needs a wire and nothing here is wired", json_schema={"type": "object"}
            )
        },
        inline=tuple(steps.values()),
        nodes=(),
        output=FromParameter(parameter="unused"),
    )


async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Stands in for a performer: `validate()` only checks that this run can perform a briefing, never runs one."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _run_code(step: Program, output_type: type, /, **inputs: object) -> object:
    """Stands in for a sandbox: `validate()` only checks that this run has one, never runs a program in it."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _decide_gate(gate_id: str, charge: str, value: object, /) -> GateDecision:
    """Stands in for an orchestrator: `validate()` only checks that this run has one, never asks it anything."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _registry(run_code: CodeRunner) -> StepRegistry:
    """A registry offering exactly what this app offers, plus the one host step the library's workflows name."""
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_perform,
        grants=grantable,
        run_code=run_code,
        decide_gate=_decide_gate,
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )

    async def notify_owner(collection: str, written: int, needs_you: int = 0) -> str:
        return f"{written} in {collection}"

    registry.register(notify_owner, name="notify_owner", replay_safe=False)
    return registry


def _skill_refusal(step: Briefing, registry_names: Collection[str]) -> str | None:
    """The refusal `step` earns for naming a skill absent from `registry_names`, or `None` when every name is on file."""
    unknown = sorted(name for name in step.skills if name not in registry_names)
    if not unknown:
        return None
    return f"step {step.name!r} names skill {unknown[0]!r}, which is not on file. The registry holds: {', '.join(sorted(registry_names))}."


async def test_every_step_file_loads_and_asks_only_for_tools_this_app_offers() -> None:
    steps = _steps()
    assert set(steps) == {
        "find_undigested",
        "digest_source",
        "extract_references",
        "report",
        "recent_digests",
        "relate",
        "flag_ideas",
        "write_flags",
        "merge_flags",
        "draft_decision_brief",
        "review_decision_brief",
        "render_decision_brief",
        "finish",
        "initiative_threads",
        "fold_notes",
        "write_analyses",
        "fold_report",
        "pending_designs",
        "draft_adr",
        "check_adrs",
        "write_adrs",
        "trace_removal",
        "excise_source",
        "check_excision",
        "removal_report",
        "repair_digest",
        "count_digests",
        "link_entities",
        "relate_digest",
    }
    offered = set(await app_grants())
    for step in steps.values():
        assert set(step.tools) <= offered, step.name
    registry_names = registry_skill_names()
    for step in steps.values():
        if isinstance(step, Briefing):
            assert _skill_refusal(step, registry_names) is None, step.name


def test_a_step_naming_an_unknown_skill_is_refused_naming_the_registry() -> None:
    registry_names = registry_skill_names()
    step = Briefing(
        name="x",
        instructions="Do the very small thing this test asks of it, nothing more.",
        output=Shape(json_schema={"type": "string"}),
        skills=("no-such-skill",),
    )

    refusal = _skill_refusal(step, registry_names)

    assert refusal is not None
    assert "names skill 'no-such-skill', which is not on file. The registry holds:" in refusal
    for name in sorted(registry_names):
        assert name in refusal


async def test_every_shipped_workflow_validates_against_a_registry_offering_the_grants_and_notify_owner() -> None:
    steps = _steps_by_id()
    for name in WORKFLOWS:
        report = validate(_shipped(name, steps), await _registry(_run_code))
        assert report.ok, (name, report.issues)


def test_a_uses_entry_naming_a_version_no_step_file_declares_is_refused() -> None:
    """A stale version must fail here too, not only at a real import that fetches the exact id."""
    steps = _steps_by_id()
    assert "st.digest_source.v11" in steps, "this case only means something while that step really ships at that version"

    with pytest.raises(AssertionError, match=r"st\.digest_source\.v10"):
        _copied(("st.digest_source.v10",), steps, where="a workflow")


def test_the_link_turn_always_reads_the_digest_from_its_file_and_the_relate_turn_links_only_backward_in_time() -> None:
    """A link worker reads every digest with read_page and never retypes it, and a relate worker reads each page's date before linking."""
    steps = _steps_by_id()
    link = steps["st.link_entities.v2"]
    relate = steps["st.relate_digest.v2"]
    assert isinstance(link, Briefing)
    assert isinstance(relate, Briefing)

    link_words = " ".join(link.instructions.split())
    relate_words = " ".join(relate.instructions.split())
    assert "read_page" in link.tools
    assert "call read_page" in link_words
    assert "pass the same object to write_note as `payload`, with its `path` as `path`" in link_words
    assert "never retype any of it" in link_words
    assert "When read_page refuses the page, write nothing and return the digest id, its status and entities_linked set to 0" in link_words
    assert "When it needs no name linked, write nothing" in link_words
    assert "earlier turn" not in link_words, "no branch for a digest written earlier in the same conversation"
    assert (
        "When write_note refuses the page naming a link field, fix exactly that link field on the payload and write again. "
        "When the refusal names anything else, write nothing and return the digest id, its status and entities_linked set to 0."
    ) in link_words
    assert "read_page" in relate.tools
    assert "print only that field's value from it, never the whole payload" in relate_words
    assert "Link a candidate only when its date is earlier than the digest's" in relate_words


def test_a_uses_entry_naming_the_old_find_undigested_is_refused() -> None:
    """The lookup that matched picked ids only among the newest 500 is gone, so a workflow still naming it fails here."""
    steps = _steps_by_id()
    assert "st.find_undigested.v4" in steps, "this case only means something while that step really ships at that version"

    with pytest.raises(AssertionError, match=r"st\.find_undigested\.v3"):
        _copied(("st.find_undigested.v3",), steps, where="a workflow")


# --- the programs, in the real sandbox ------------------------------------------------------

_FLAGS = [
    {
        "title": "Try planning reminders",
        "summary": "Compaction drops the plan, so mount the planning capability.",
        "flag_kind": "idea",
        "about": ["talk.agents-that-plan"],
        "why": "Compaction drops the plan.",
        "proposal": "Mount the planning capability.",
        "evidence": "talk.agents-that-plan#findings",
    },
    {
        "title": "Watch the context budget",
        "summary": "Re-injection costs tokens every turn, so cap the re-injected list.",
        "flag_kind": "risk",
        "about": ["talk.agents-that-plan"],
        "why": "Re-injection costs tokens every turn.",
        "proposal": "Cap the re-injected list.",
        "evidence": "",
    },
]

_BRIEF: dict[str, object] = {
    "title": "Whether to raise the retrieval result window from five to ten",
    "question": "Should the assistant read ten retrieved results per question instead of five?",
    "summary": "Keep five. The reading shows accuracy flattening past roughly five results while cost keeps rising, and the one paper that gained from ten used far longer documents than ours.",
    "decision": "keep_current",
    "recommendation": "Leave the window at five and spend the budget on better ranking instead.",
    "evidence": [
        {
            "ref": "page:research/paper.long-context-degradation",
            "statement": "Answer accuracy stops improving past roughly five retrieved passages and falls once the prompt grows past half the window.",
            "role": "support",
            "conditions": "Measured on question answering over passages of about 400 tokens, close to ours.",
            "quote": "Beyond five passages the gain is within noise, and past half the context window accuracy declines.",
            "locator": "section 4.2",
        },
        {
            "ref": "page:research/talk.ranking-beats-recall",
            "statement": "Reordering the same five results by a cross-encoder gained more than doubling how many were read.",
            "role": "support",
            "conditions": "Holds where a reranker is affordable at query time, which ours is.",
        },
        {
            "ref": "page:research/paper.ten-passage-gain",
            "statement": "One study did gain from ten passages rather than five.",
            "role": "counterevidence",
            "conditions": "Its documents averaged 3,000 tokens, so a single passage rarely held a whole answer. Ours are short.",
        },
    ],
    "alternatives": [
        "Raise the window to ten and cap the prompt by truncating each result, which loses the end of every long passage.",
        "Keep five and add a reranker, which is the next action rather than an alternative to it.",
    ],
    "limitations": "Nothing read here measures a window above ten, and no source covers questions that need several documents at once.",
    "next_action": "Try a reranker over the existing five results and measure answer accuracy against today's ordering.",
}
"""One reviewed brief, shaped the way `draft_decision_brief` answers, for the renderer to join."""

_PROGRAM_INPUTS: dict[str, dict[str, object]] = {
    "find_undigested": {"collection": "research", "picked_sources": ""},
    "recent_digests": {"collection": "research", "digest_ids": ""},
    "report": {
        "digests": [
            {"digest_id": "talk.a", "status": "written", "edges_added": 3},
            {"digest_id": "talk.b", "status": "skipped", "edges_added": 0},
        ],
        "references": [
            [
                {"url": "https://arxiv.org/abs/2401.01234", "why": "cited", "decision": "requested", "reason": "builds on it"},
                {"url": "https://arxiv.org/abs/2401.00002", "why": "cited", "decision": "seen", "reason": "beyond hop depth 1"},
                {"url": "https://github.com/x/y", "why": "code", "decision": "dropped_by_rule", "reason": "repository"},
                {"why": "no url", "decision": "no_url", "reason": "search for it by title"},
            ],
            [],
        ],
    },
    "count_digests": {
        "digests": [
            {"digest_id": "dg.a", "status": "written"},
            {"digest_id": "dg.b", "status": "skipped", "reason": "Skipped: no finding bears on the purpose."},
            {"digest_id": "dg.c", "status": "written"},
        ]
    },
    "write_flags": {"approved": {"flags": _FLAGS, "defects": []}, "collection": "research"},
    "finish": {"notified": "3 in research"},
    "initiative_threads": {"collection": "research"},
    "write_analyses": {
        "drafts": [
            {
                "folder": "initiatives/2026-08-29-x",
                "title": "X",
                "summary": "A short summary of the design in initiative x.",
                "sections": [{"heading": "Findings", "body": "F1. The owner asked why this design, answered in the design page."}],
                "folded": [{"path": "initiatives/2026-08-29-x/design.md", "note_id": "n1", "finding": "F1"}],
            }
        ],
        "initiatives": [
            {
                "folder": "initiatives/2026-08-29-x",
                "opening_status": "active",
                "analysis_exists": False,
                "analysis_text": "",
                "files": [],
            }
        ],
        "collection": "research",
    },
    "fold_report": {"written": [{"folder": "initiatives/2026-08-29-x", "page_id": "bs.x", "resolved": 1}]},
    "render_decision_brief": {"brief": _BRIEF},
    "pending_designs": {"collection": "research"},
    "write_adrs": {
        "drafts": [
            {
                "design_id": "spec-2026-08-29-x",
                "slug": "something",
                "title": "X",
                "summary": "A short summary of the design.",
                "sections": [
                    {"heading": "Context", "body": "The initiative that led here."},
                    {"heading": "Decision", "body": "What we decided."},
                    {"heading": "Consequences", "body": "What follows."},
                ],
            }
        ],
        "collection": "research",
    },
    "removal_report": {
        "plan": {
            "source_id": "src-1",
            "only_x_summary": "",
            "expected_paths": ["mirror.md", "digest.md"],
            "rewrites": [{"page_id": "p1", "path": "p1.md", "why": "quotes it", "excise": "the quoted lines"}],
            "heals": [{"page_id": "p2", "path": "p2.md", "drop_link_to": "src-1"}],
            "nothing_found": False,
        },
        "results": [
            {"page_id": "p1", "status": "rewritten", "detail": "removed the quoted lines"},
            {"page_id": "p2", "status": "healed", "detail": "dropped the link"},
            {"page_id": "p3", "status": "deferred", "detail": "filed under decision.review as decision.the-source-in-full"},
            {"page_id": "p4", "status": "deferred", "detail": "could not cleanly excise, no decision page type declared"},
        ],
        "removal_reply": "removed src-1 from research: 2 file(s) gone, ledger blocked, reindexed",
        "collection": "research",
    },
}
"""One set of inputs per shipped program, shaped like the node that feeds it."""

_OPEN_THREAD: dict[str, object] = {
    "note_id": "n1",
    "status": "open",
    "anchor": "the chosen design",
    "text": "why this design and not the alternative?",
    "created_at": "2026-08-29T00:00:00Z",
    "replies": [],
}
"""The one open thread `read_notes` answers with, for every path asked."""

_UNDIGESTED: list[dict[str, str]] = [
    {"source_id": "yt-1", "provider": "intake", "item_type": "video", "title": "Agents that plan"},
    {"source_id": "arxiv-2401.01234", "provider": "intake", "item_type": "paper", "title": "A paper"},
    {"source_id": "url-2", "provider": "intake", "item_type": "article", "title": "An article"},
]
"""Every undigested mirror here, newest first."""

_TOOL_ANSWERS: dict[str, object] = {
    "write_note": "written",
    "list_undigested": _UNDIGESTED,
    "read_notes": {"path": "", "exists": True, "text": "# Design\n", "threads": [_OPEN_THREAD]},
    "resolve_note": "resolved",
    "list_pages": [
        {"page_id": "adr-0030-something", "page_type": "adr", "title": "Something", "status": "accepted"},
        {"page_id": "engram-adr-0044-elsewhere", "page_type": "adr", "title": "Elsewhere", "status": "accepted"},
    ],
}
"""What a granted tool answers here. The programs only have to survive the shape."""

_COMPOUNDING_ANSWERS: dict[str, dict[str, object]] = {
    "digest-sources": {
        "entry": {"name": "digest-sources", "to": ["talk"], "cap_per_run": 40, "gate": "none", "guidance": ""},
        "opening_status": "active",
        "rows": [{"source_id": "yt-1", "provider": "intake", "item_type": "video", "title": "Agents that plan"}],
    },
    "flag-ideas": {
        "entry": {"name": "flag-ideas", "to": ["flag"], "cap_per_run": 40, "gate": "owner", "guidance": ""},
        "opening_status": "pending",
        "rows": [
            {
                "page_id": "talk.agents-that-plan",
                "page_type": "talk",
                "title": "Agents that plan",
                "status": "active",
                "source_id": "yt-1",
                "path": "Wiki/Talks/agents-that-plan.md",
                "findings": [],
            }
        ],
    },
    "notes-to-analysis": {
        "entry": {"name": "notes-to-analysis", "to": ["brainstorming-doc"], "cap_per_run": 40, "gate": "none", "guidance": ""},
        "opening_status": "active",
        "rows": [{"path": "initiatives/2026-08-29-x/design.md", "open_notes": 1}],
    },
    "promote-initiative": {
        "entry": {"name": "promote-initiative", "to": ["adr"], "cap_per_run": 40, "gate": "owner", "guidance": ""},
        "opening_status": "proposed",
        "rows": [{"page_id": "spec-2026-08-29-x", "page_type": "spec-doc", "title": "X", "status": "executed"}],
    },
}
"""What `compounding_work` answers here, one answer per policy entry the shipped steps name."""


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


_CalledByStep = dict[str, list[tuple[str, dict[str, object]]]]


def _assert_research_pass_programs(ran: dict[str, object], called_by_step: _CalledByStep) -> None:
    """The five original programs: their outputs, and which tools they called, in order."""
    assert to_jsonable_python(ran["find_undigested"]) == [{"source_id": "yt-1", "item_type": "video", "title": "Agents that plan"}]
    assert to_jsonable_python(ran["recent_digests"]) == [
        {
            "group_id": 0,
            "members": [
                {
                    "digest_id": "talk.agents-that-plan",
                    "page_type": "talk",
                    "title": "Agents that plan",
                    "source_id": "yt-1",
                    "path": "Wiki/Talks/agents-that-plan.md",
                    "findings": [],
                }
            ],
        }
    ]
    assert to_jsonable_python(ran["report"]) == {
        "written": 1,
        "skipped": 1,
        "requested": 1,
        "seen": 1,
        "dropped_by_rule": 1,
        "dropped_by_judgment": 0,
        "no_url": 1,
        "edges_added": 3,
    }
    assert ran["write_flags"] == 2, "one id per flag, and every one of them is an awaited write_note"
    assert to_jsonable_python(ran["finish"]) == {"notified": "3 in research"}

    assert [name for name, _ in called_by_step["find_undigested"]] == ["compounding_work"]
    assert called_by_step["find_undigested"][0][1] == {"knowledge_base": "research", "name": "digest-sources"}
    assert [name for name, _ in called_by_step["recent_digests"]] == ["compounding_work"]
    assert called_by_step["recent_digests"][0][1] == {"knowledge_base": "research", "name": "flag-ideas"}
    assert called_by_step["report"] == []
    assert called_by_step["finish"] == []

    written = [to_jsonable_python(args)["payload"]["frontmatter"] for name, args in called_by_step["write_flags"] if name == "write_note"]
    assert [page["id"] for page in written] == ["flag.try-planning-reminders", "flag.watch-the-context-budget"]
    assert [page["status"] for page in written] == ["pending", "pending"]


def _assert_benchmark_pass_programs(ran: dict[str, object], called_by_step: _CalledByStep) -> None:
    """The benchmark digest's own count: digests written and skipped, with no tool calls."""
    assert to_jsonable_python(ran["count_digests"]) == {"written": 2, "skipped": 1}
    assert called_by_step["count_digests"] == []


def _assert_docs_pass_programs(ran: dict[str, object], called_by_step: _CalledByStep) -> None:
    """The six new programs: their outputs, and which tools they called, in order, with exact write args."""
    assert to_jsonable_python(ran["initiative_threads"]) == [
        {
            "folder": "initiatives/2026-08-29-x",
            "opening_status": "active",
            "analysis_exists": True,
            "analysis_text": "# Design\n",
            "files": [
                {
                    "path": "initiatives/2026-08-29-x/design.md",
                    "text": "# Design\n",
                    "threads": [_OPEN_THREAD],
                }
            ],
        }
    ]
    assert to_jsonable_python(ran["write_analyses"]) == [{"folder": "initiatives/2026-08-29-x", "page_id": "written", "resolved": 1}]
    assert to_jsonable_python(ran["fold_report"]) == {"initiatives": 1, "threads_resolved": 1}
    assert to_jsonable_python(ran["pending_designs"]) == [{"page_id": "spec-2026-08-29-x", "title": "X"}]
    assert ran["write_adrs"] == 1, "one id per draft, and every one of them is an awaited write_note"

    assert called_by_step["fold_report"] == []
    assert [name for name, _ in called_by_step["initiative_threads"]] == ["compounding_work", "read_notes", "read_notes"]
    assert called_by_step["initiative_threads"][0][1] == {"knowledge_base": "research", "name": "notes-to-analysis"}
    assert [name for name, _ in called_by_step["pending_designs"]] == ["compounding_work"]
    assert called_by_step["pending_designs"][0][1] == {"knowledge_base": "research", "name": "promote-initiative"}

    assert [name for name, _ in called_by_step["write_analyses"]] == ["write_note", "resolve_note"]
    write_analysis_call, resolve_thread_call = called_by_step["write_analyses"]
    assert to_jsonable_python(write_analysis_call[1]) == {
        "knowledge_base": "research",
        "payload": {
            "frontmatter": {
                "id": "bs.x",
                "aliases": ["x", "bs.x"],
                "type": "brainstorming-doc",
                "title": "X",
                "summary": "A short summary of the design in initiative x.",
                "status": "active",
            },
            "sections": [{"heading": "Findings", "body": "F1. The owner asked why this design, answered in the design page."}],
        },
        "path": "initiatives/2026-08-29-x/analysis.md",
    }
    assert to_jsonable_python(resolve_thread_call[1]) == {
        "knowledge_base": "research",
        "path": "initiatives/2026-08-29-x/design.md",
        "note_id": "n1",
        "reply": "Folded into analysis.md as F1.",
    }

    assert [name for name, _ in called_by_step["write_adrs"]] == ["compounding_work", "list_pages", "write_note"]
    assert called_by_step["write_adrs"][0][1] == {"knowledge_base": "research", "name": "promote-initiative"}
    assert called_by_step["write_adrs"][1][1] == {"knowledge_base": "research", "page_type": "adr"}
    write_adr_call = called_by_step["write_adrs"][2]
    assert to_jsonable_python(write_adr_call[1]) == {
        "knowledge_base": "research",
        "payload": {
            "frontmatter": {
                "id": "adr-0031-something",
                "aliases": ["adr-0031-something"],
                "type": "adr",
                "app": "root",
                "status": "proposed",
                "title": "X",
                "summary": "A short summary of the design.",
                "absorbs": ["[[spec-2026-08-29-x]]"],
            },
            "sections": [
                {"heading": "Context", "body": "The initiative that led here."},
                {"heading": "Decision", "body": "What we decided."},
                {"heading": "Consequences", "body": "What follows."},
            ],
        },
        "path": "adr/0031-something.md",
    }


def _assert_source_remove_programs(ran: dict[str, object], called_by_step: _CalledByStep) -> None:
    """The one source-removal program among the library files: pure arithmetic, no tool calls of its own.

    Two deferred results exercise the summary's own join: each carries its `page_id` and `detail`
    (one as a knowledge base would file it under a decision kind, one as it would read with no decision
    kind declared -- the report treats both the same way), comma-joined, never with a semicolon.
    """
    assert to_jsonable_python(ran["removal_report"]) == {
        "removed": "src-1",
        "files_gone": 2,
        "rewritten": 1,
        "healed": 1,
        "deferred": 2,
        "summary": "Removed src-1 from research: 1 rewritten, 1 healed, 2 deferred, 2 files gone. "
        "removed src-1 from research: 2 file(s) gone, ledger blocked, reindexed Deferred: "
        "p3: filed under decision.review as decision.the-source-in-full, "
        "p4: could not cleanly excise, no decision page type declared.",
    }
    assert called_by_step["removal_report"] == []


def _assert_decision_brief_renderer(ran: dict[str, object], called_by_step: _CalledByStep) -> None:
    """The deterministic renderer: Markdown for the person, a proposed note the consumer's own model accepts, and no calls at all."""
    rendered = to_jsonable_python(ran["render_decision_brief"])
    assert isinstance(rendered, dict)
    document = rendered["document"]
    assert isinstance(document, str)
    assert document.startswith("# Whether to raise the retrieval result window from five to ten")
    assert "**Decision.** Keep the current course" in document
    assert "**Arguing the other way**" in document, "the counterevidence keeps its own place rather than being folded in"
    assert "page:research/paper.long-context-degradation, section 4.2" in document, "the ref is the one the brief carried"
    assert "Holds when: Its documents averaged 3,000 tokens" in document

    proposed = NoteDraft.model_validate(rendered["note_draft"])
    assert proposed.page_type == "question"
    assert dict(proposed.frontmatter) == {"status": "synthesized", "question_status": "answered"}
    assert [section.heading for section in proposed.sections] == ["TL;DR", "Why It Matters", "Current Evidence", "Candidate Answers"]
    assert proposed.summary == _BRIEF["summary"]
    assert called_by_step["render_decision_brief"] == [], "the renderer reads nothing and writes nothing"


def _title_bound(schema: Mapping[str, object]) -> int:
    """The `maxLength` one JSON Schema puts on its `title` property."""
    properties = schema["properties"]
    assert isinstance(properties, dict), "a schema with properties"
    title = properties["title"]
    assert isinstance(title, dict), "a title property"
    bound = title.get("maxLength")
    assert isinstance(bound, int), "a title bounded by a number"
    return bound


def test_the_brief_declares_the_same_title_bound_the_note_model_enforces() -> None:
    """The producer's declared title and the consumer's own bound are one number, compared rather than each checked alone.

    The brief's title travels into the rendered note's title, so a bound on one side and not the other is a run
    that completes and a keep that refuses.
    """
    declared = _steps()["draft_decision_brief"].output
    assert isinstance(declared, Shape), "the brief declares its own output shape"
    assert _title_bound(declared.json_schema) == _title_bound(NoteDraft.model_json_schema())


async def test_every_shipped_program_runs_in_the_real_sandbox_and_calls_the_tools_it_names(sandbox: AsyncMonty) -> None:
    """The test that catches a granted tool called without `await`: the sandbox renders every one of them async.

    Runs every program in `workflows/steps/*.md` directly, not just the ones the two shipped
    workflows' `uses` lists happen to reference, so a step file no workflow wires yet still runs.
    """
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        if name == "compounding_work":
            return _COMPOUNDING_ANSWERS[str(args["name"])]
        return _TOOL_ANSWERS[name]

    steps = _steps()
    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_bring_every_step(_shelf_and_workflow_glue(steps)), await _registry(runner))
    assert ready.report.ok, ready.report.issues

    ran: dict[str, object] = {}
    called_by_step: _CalledByStep = {}
    for step_name, step in steps.items():
        if not isinstance(step, Program):
            continue
        built = ready.registry.get(step_name)
        assert built is not None, step_name
        before = len(called)
        ran[step_name] = await built.function(**_PROGRAM_INPUTS[step_name])
        called_by_step[step_name] = called[before:]

    assert set(ran) == {
        "find_undigested",
        "recent_digests",
        "report",
        "write_flags",
        "finish",
        "initiative_threads",
        "write_analyses",
        "fold_report",
        "pending_designs",
        "write_adrs",
        "removal_report",
        "render_decision_brief",
        "count_digests",
    }
    _assert_research_pass_programs(ran, called_by_step)
    _assert_benchmark_pass_programs(ran, called_by_step)
    _assert_docs_pass_programs(ran, called_by_step)
    _assert_source_remove_programs(ran, called_by_step)
    _assert_decision_brief_renderer(ran, called_by_step)


async def test_find_undigested_with_picked_sources_answers_exactly_those_off_the_undigested_list(sandbox: AsyncMonty) -> None:
    """The subset path: picked ids go to `list_undigested` as `source_ids` with no limit, never the policy batch."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        if name == "compounding_work":
            return _COMPOUNDING_ANSWERS[str(args["name"])]
        picked = args.get("source_ids")
        if name == "list_undigested" and isinstance(picked, list):
            return [row for row in _UNDIGESTED if row["source_id"] in picked]
        return _TOOL_ANSWERS[name]

    steps = _steps()
    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_bring_every_step(_shelf_and_workflow_glue(steps)), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("find_undigested")
    assert built is not None

    rows = await built.function(collection="research", picked_sources=" url-2, yt-1 , not-a-mirror")

    assert to_jsonable_python(rows) == [
        {"source_id": "yt-1", "item_type": "video", "title": "Agents that plan"},
        {"source_id": "url-2", "item_type": "article", "title": "An article"},
    ]
    assert called == [("list_undigested", {"knowledge_base": "research", "source_ids": ["url-2", "yt-1", "not-a-mirror"]})]


async def test_recent_digests_with_digest_ids_reads_exactly_those_through_page_ids(sandbox: AsyncMonty) -> None:
    """Named digests are the batch, whatever their age: the ids reach compounding_work as page_ids."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return _COMPOUNDING_ANSWERS[str(args["name"])]

    steps = _steps()
    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_bring_every_step(_shelf_and_workflow_glue(steps)), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("recent_digests")
    assert built is not None

    await built.function(collection="research", digest_ids=" talk.agents-that-plan, paper.x ")

    assert called == [("compounding_work", {"knowledge_base": "research", "name": "flag-ideas", "page_ids": ["talk.agents-that-plan", "paper.x"]})]


async def test_news_relate_lists_every_active_digest_of_the_base_past_the_listing_s_default_cap(sandbox: AsyncMonty) -> None:
    """The base holds 609 digests and `list_pages` stops at 500 by default, so the lister asks for far more at once."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return [
            {"page_id": "dg.apple-cuts-prices", "page_type": "digest", "title": "Apple cuts prices", "status": "active"},
            {"page_id": "dg.ftx-trial-opens", "page_type": "digest", "title": "FTX trial opens", "status": "active"},
        ]

    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_shipped("news-relate", _steps_by_id()), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("list_digests")
    assert built is not None

    ids = await built.function(collection="multihop-rag")

    assert to_jsonable_python(ids) == ["dg.apple-cuts-prices", "dg.ftx-trial-opens"]
    assert called == [("list_pages", {"knowledge_base": "multihop-rag", "page_type": "digest", "status": "active", "limit": 1000})]


async def test_news_link_lists_every_active_digest_lacking_a_mentions_link_as_a_written_digest(sandbox: AsyncMonty) -> None:
    """Each listed page reaches the link turn in the shape the digest turn hands it, so the map can feed it."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return [{"page_id": "dg.apple-cuts-prices", "page_type": "digest", "title": "Apple cuts prices", "status": "active"}]

    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_shipped("news-link", _steps_by_id()), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("list_unlinked_digests")
    assert built is not None

    digests = await built.function(collection="multihop-rag")

    assert to_jsonable_python(digests) == [{"digest_id": "dg.apple-cuts-prices", "status": "written", "reason": None}]
    assert called == [
        (
            "list_pages",
            {"knowledge_base": "multihop-rag", "page_type": "digest", "status": "active", "limit": 1000, "lacking_link": "mentions"},
        )
    ]


async def test_news_link_counts_the_digests_that_gained_links_and_the_entities_they_link(sandbox: AsyncMonty) -> None:
    """The tail of the link pass totals each digest's link result, a digest that gained no link left out of `linked`, with no tool calls."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return _TOOL_ANSWERS[name]

    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_shipped("news-link", _steps_by_id()), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("count_linked_digests")
    assert built is not None

    report = await built.function(
        digests=[
            {"digest_id": "dg.apple-cuts-prices", "status": "written", "entities_linked": 4},
            {"digest_id": "dg.nothing-named", "status": "written", "entities_linked": 0},
            {"digest_id": "dg.ftx-trial-opens", "status": "written", "entities_linked": 2},
        ]
    )

    assert to_jsonable_python(report) == {"linked": 2, "entities_linked": 6}
    assert called == []


async def test_news_digest_counts_the_digests_written_and_skipped(sandbox: AsyncMonty) -> None:
    """The tail of the digest pass reads each source worker's one result, with no tool calls and nothing about links."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return _TOOL_ANSWERS[name]

    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_shipped("news-digest", _steps_by_id()), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("count_written_digests")
    assert built is not None

    report = await built.function(
        digests=[
            {"digest_id": "dg.apple-cuts-prices", "status": "written"},
            {"digest_id": "dg.off-topic", "status": "skipped", "reason": "Skipped: nothing on the purpose"},
            {"digest_id": "dg.ftx-trial-opens", "status": "written"},
        ]
    )

    assert to_jsonable_python(report) == {"written": 2, "skipped": 1}
    assert called == []


def test_news_digest_only_writes_and_news_link_links_every_digest() -> None:
    """The digest worker takes one turn and brings no link step, and the link pass maps the link step over every digest."""
    digest = _shipped("news-digest", _steps_by_id())
    link = _shipped("news-link", _steps_by_id())

    digest_maps = [node for node in digest.nodes if isinstance(node, MapNode)]
    link_maps = [node for node in link.nodes if isinstance(node, MapNode)]
    assert [node.turns for node in digest_maps] == [("digest_source",)]
    assert "link_entities" not in {step.name for step in digest.inline}
    assert [node.turns for node in link_maps] == [("link_entities",)]


async def test_news_relate_counts_the_digests_related_and_the_links_they_gained(sandbox: AsyncMonty) -> None:
    """The tail of the relate pass totals each digest's result for the card, with no tool calls."""
    called: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        called.append((name, dict(args)))
        return _TOOL_ANSWERS[name]

    runner = code_runner(sandbox, reads=await app_reads(), call=call)
    ready = prepare(_shipped("news-relate", _steps_by_id()), await _registry(runner))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("count_related_digests")
    assert built is not None

    report = await built.function(
        results=[
            {"digest_id": "dg.ftx-trial-verdict", "edges_added": 2},
            {"digest_id": "dg.apple-cuts-prices", "edges_added": 0},
            {"digest_id": "dg.ftx-trial-opens", "edges_added": 1},
        ]
    )

    assert to_jsonable_python(report) == {"related": 3, "linked": 2, "edges_added": 3}
    assert called == []
