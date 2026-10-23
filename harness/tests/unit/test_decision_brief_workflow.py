"""The shipped decision-brief workflow, run end to end off its own files.

The graph, the steps and the result declaration come from the real documents under
`templates/research/workflows`, never from a copy typed out here: a file edited in the template
and not here would otherwise keep passing. The drafting step and the reviewer are scripted, so
no model is reached. The rendering step is not scripted, because it is plain code and running it
for real is the only way to know what a person would actually read.

What the cases cover: a rejection carrying the reviewer's exact words and the digest of the
draft it refused, a reviewer that cannot judge, a brief whose source cannot be read, a brief
that left the counterevidence out, a brief that concludes the current course holds, a brief the
sources do not settle, and a reviewer whose budget runs out. Last, the separation the whole
journey rests on: a completed run produces artifacts, and nobody's acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.artifacts import ArtifactRef
from core.ai.evaluation.contracts import Assessment, ReviewedTaskOutcome, TaskOutcome
from core.ai.workflows.contracts import (
    CURRENT_STEP,
    InlineStep,
    Program,
    StepRegistry,
    Workflow,
    WorkflowPresentation,
    canonical_reviewed_value,
    load_spec_document,
    load_step_document,
    prepare,
    reviewed_value_digest,
    run_workflow,
    step_page_id,
)
from engram.indexing.contracts import RetrievalSwitches
from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.testing.specs import research_spec
from pydantic import BaseModel, JsonValue
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS, NOT_FOR_STEPS
from tests.support.grants import app_grants, app_knowledge_base_surface, app_reads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from core.ai.workflows.contracts import Briefing, CodeRunner, GateDecision
    from core.ai.workflows.registry import StepImplementation

RESEARCH = Path(__file__).resolve().parents[3] / "templates" / "research" / "workflows"

PARAMS: dict[str, JsonValue] = {
    "question": "Should the assistant read ten retrieved results per question instead of five?",
    "knowledge_bases": ["research"],
    "project_context": "A question-answering assistant over short passages, reading five results today.",
    "constraints": "The answer has to stay inside the current per-question cost.",
}
"""One decision as a person would state it, the same for every case here."""

_EVIDENCE = {
    "ref": "page:research/paper.long-context-degradation",
    "statement": "Accuracy stops improving past roughly five retrieved passages.",
    "role": "support",
    "conditions": "Measured over passages of about 400 tokens, close to this project's.",
    "quote": "Beyond five passages the gain is within noise.",
    "locator": "section 4.2",
}

_COUNTEREVIDENCE = {
    "ref": "page:research/paper.ten-passage-gain",
    "statement": "One study did gain from ten passages rather than five.",
    "role": "counterevidence",
    "conditions": "Its documents averaged 3,000 tokens, so one passage rarely held a whole answer.",
}


def _brief(**changes: object) -> dict[str, object]:
    """A complete brief, with whatever this case changes about it."""
    brief: dict[str, object] = {
        "title": "Whether to raise the retrieval result window from five to ten",
        "question": str(PARAMS["question"]),
        "summary": "Keep five. Accuracy flattens past roughly five results while cost keeps rising.",
        "decision": "keep_current",
        "recommendation": "Leave the window at five and spend the budget on better ranking instead.",
        "evidence": [_EVIDENCE, _COUNTEREVIDENCE],
        "alternatives": ["Raise the window to ten and truncate each result, which loses the end of every long passage."],
        "limitations": "Nothing read here measures a window above ten.",
        "next_action": "Try a reranker over the existing five results and measure answer accuracy.",
    }
    brief.update(changes)
    return brief


def _steps_by_id() -> dict[str, InlineStep]:
    """Every research step file, under the id the library mints for it."""
    documents = [load_step_document(path.read_text(encoding="utf-8")) for path in sorted((RESEARCH / "steps").glob("*.md"))]
    return {step_page_id(doc.step.name, doc.version): doc.step for doc in documents}


def _spec() -> Workflow:
    """The shipped workflow with the steps its `uses` names copied in, exactly as an import copies them."""
    text = (RESEARCH / "workflows" / "research-decision-brief.md").read_text(encoding="utf-8")
    spec = load_spec_document(text).workflow
    by_id = _steps_by_id()
    uses = list(yaml.safe_load(text.split("---", 2)[1]).get("uses", []))
    missing = [step_id for step_id in uses if step_id not in by_id]
    assert not missing, f"the workflow names {missing}, which no shipped step file declares"
    return spec.model_copy(update={"inline": (*spec.inline, *(by_id[step_id] for step_id in uses))})


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """A performer that must never run: every briefing here is answered through the executor seam."""
    del output_type, inputs
    msg = f"the scripted executor answers for {task.name!r}"
    raise AssertionError(msg)


async def _no_gate_decider(gate_id: str, charge: str, value: object, /) -> GateDecision:
    """Stands in for an orchestrator. This workflow declares no orchestrator gate, so it is never asked."""
    del charge, value
    msg = f"nothing should decide {gate_id!r} here"
    raise AssertionError(msg)


async def _registry(run_code: CodeRunner) -> StepRegistry:
    """A registry offering exactly what this app offers a declared step, with a real sandbox behind the programs."""
    grantable = await app_grants()
    return StepRegistry(
        perform=_never_performed,
        grants=grantable,
        run_code=run_code,
        decide_gate=_no_gate_decider,
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )


@dataclass
class Scripted:
    """The drafting step and the reviewer, scripted. The renderer runs for real.

    `drafts` and `verdicts` are consumed one per call, so a case scripts a rejection by giving
    the reviewer two verdicts and the drafting step two briefs. `blow_budget` makes the reviewer
    spend its allowance instead of answering, the way a real run refuses past a limit.
    `repairs` records what each drafting call was told it owed, so a case can read the exact
    words and the exact digest the reviewer refused.
    """

    drafts: list[dict[str, object]]
    verdicts: list[dict[str, object]]
    blow_budget: bool = False
    calls: list[str] = field(default_factory=list)
    repairs: list[tuple[str, str, str, int]] = field(default_factory=list)
    drafted: list[object] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append(node_id)
        if node_id == "draft":
            identity = CURRENT_STEP.get()
            for owed in identity.retry_feedback if identity is not None else ():
                self.repairs.append((owed.gate_id, owed.feedback, owed.reviewed.value_digest, owed.attempt))
            answer = _validated(implementation.output_type, self.drafts.pop(0))
            self.drafted.append(answer)
            return answer
        if node_id == "review":
            if self.blow_budget:
                msg = "The next tool call(s) would exceed the tool_calls_limit of 20 (tool_calls=22)."
                raise UsageLimitExceeded(msg)
            return _validated(implementation.output_type, self.verdicts.pop(0))
        return await implementation.function(**kwargs)


def _validated(output_type: type, payload: dict[str, object]) -> object:
    """`payload` as the step's own declared output type, so a scripted answer is typed like a real one."""
    assert issubclass(output_type, BaseModel), output_type
    return output_type.model_validate(payload)


@dataclass
class Reviewer:
    """The person a gate escalates to. Records the note it was handed, and approves."""

    reviewed: list[tuple[str, str]] = field(default_factory=list)

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del value
        self.reviewed.append((gate_id, note))
        return True


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


async def _run(sandbox: AsyncMonty, scripted: Scripted, *, reviewer: Reviewer | None = None) -> object:
    """One whole run of the shipped workflow against `scripted`, with the renderer really rendering."""

    async def call(name: str, args: dict[str, object], /) -> object:
        del args
        msg = f"the renderer reads nothing, so {name!r} must never be called"
        raise AssertionError(msg)

    registry = await _registry(code_runner(sandbox, reads=await app_reads(), call=call))
    return await run_workflow(
        _spec(), registry, params=PARAMS, approval=reviewer if reviewer is not None else Reviewer(), executor=scripted
    )


def _rendered(result: object) -> dict[str, object]:
    """One finished run's value as plain data."""
    payload = to_jsonable_python(result)
    assert isinstance(payload, dict)
    return payload


def _document(result: object) -> str:
    """The Markdown half of a finished run, the part a person reads."""
    document = _rendered(result)["document"]
    assert isinstance(document, str)
    return document


# --- the files themselves ---------------------------------------------------------------------


async def test_the_shipped_files_load_and_the_graph_checks() -> None:
    """Every step the workflow names resolves, and the whole graph wires clean against this app's own grants."""
    spec = _spec()
    ready = prepare(spec, await _registry(_unreachable_sandbox))

    assert ready.report.ok, ready.report.issues
    assert [node.id for node in spec.nodes] == ["draft", "review", "present"]
    judge = next(node for node in spec.nodes if node.id == "review")
    assert judge.kind == "judge"
    assert judge.rejected_to == "draft", "a rejection sends the drafting round again, never the renderer"
    assert judge.approved_when == "verdict"
    renderer = next(step for step in spec.inline if step.name == "render_decision_brief")
    assert isinstance(renderer, Program), "the last stage is code, so a reviewed conclusion is never rewritten by a model"


async def _unreachable_sandbox(step: Program, output_type: type, /, **inputs: object) -> object:
    """A sandbox the validator only has to find, never use."""
    del output_type, inputs
    msg = f"nothing should run {step.name!r} here"
    raise AssertionError(msg)


async def _earned() -> RetrievalSwitches:
    """The switches a knowledge base with dense enough relations decides for itself, for the counterfactual below."""
    return RetrievalSwitches(embeddings=True, graph=True)


async def _step_tools_of(knowledge_base: RetrievalKnowledgeBase[object]) -> frozenset[str]:
    """What a declared step may be granted over `knowledge_base`, derived the way the app derives it."""
    return frozenset(tool.name for tool in await knowledge_base.list_tools() if tool.name not in NOT_FOR_STEPS)


async def test_the_workflow_asks_for_nothing_the_research_template_does_not_grant() -> None:
    """A base started from this template grants what its own schema earns, and this workflow lives inside that.

    The graph reads are the case that matters. Engram registers `graph_expand` and `find_paths` only over a
    surface that can earn them, and a surface earns them only in auto retrieval mode. The research template
    declares fixed, so a step file naming either would validate against a hand-widened grant surface and be
    refused by a real import of this folder.
    """
    spec = research_spec()
    assert spec.retrieval.mode == "fixed", "this case is about what a fixed-mode knowledge base grants"
    reads = await _step_tools_of(app_knowledge_base_surface())
    grantable = frozenset(await app_grants())

    assert {"graph_expand", "find_paths"}.isdisjoint(reads), "a fixed-mode base offers no graph read"
    for step in _spec().inline:
        assert set(step.tools) <= grantable, f"{step.name} asks for {sorted(set(step.tools) - grantable)}"
    ready = prepare(_spec(), await _registry(_unreachable_sandbox))
    assert ready.report.ok, ready.report.issues


async def test_the_graph_reads_are_absent_because_of_the_mode_and_not_the_grant_surface() -> None:
    """The same surface, told it earned the graph, offers both. The absence above is the template's own doing."""
    fixed = app_knowledge_base_surface()
    auto = RetrievalKnowledgeBase[object](
        surfaces=tuple(replace(surface, switch_source=_earned) for surface in fixed.surfaces), authoring=fixed.authoring
    )

    assert {"graph_expand", "find_paths"} <= await _step_tools_of(auto)
    assert {"graph_expand", "find_paths"}.isdisjoint(await _step_tools_of(fixed))


# --- the review loop --------------------------------------------------------------------------


async def test_a_rejection_reruns_the_drafting_step_with_the_reviewers_words_and_the_refused_draft(sandbox: AsyncMonty) -> None:
    """The second attempt is handed the exact feedback and the digest of the exact brief that earned it."""
    refused = _brief(recommendation="Raise the window to ten, the reading supports it.", decision="recommend_change")
    repaired = _brief()
    feedback = "page:research/paper.ten-passage-gain argues the other way and the brief never states it. Add it as counterevidence."
    scripted = Scripted(drafts=[refused, repaired], verdicts=[{"verdict": "reject", "feedback": feedback}, {"verdict": "approve", "feedback": ""}])

    result = await _run(sandbox, scripted)

    assert scripted.calls == ["draft", "review", "draft", "review", "present"], "the rejection rewinds the drafting step alone"
    assert len(scripted.repairs) == 1, "only the second attempt owes a repair"
    gate_id, said, digest, attempt = scripted.repairs[0]
    assert (gate_id, said, attempt) == ("review", feedback, 1)
    refused_digest = reviewed_value_digest(canonical_reviewed_value(scripted.drafted[0]))
    assert digest == refused_digest, "the digest names the brief that was refused, not the repaired one"
    assert digest != reviewed_value_digest(canonical_reviewed_value(scripted.drafted[1]))
    assert "Keep the current course" in _document(result)


async def test_a_reviewer_that_cannot_judge_hands_the_brief_to_the_person(sandbox: AsyncMonty) -> None:
    """Not a rejection and not an approval: the run waits for a person, carrying what the reviewer saw."""
    scripted = Scripted(
        drafts=[_brief()],
        verdicts=[{"verdict": "cannot_judge", "feedback": "The knowledge base named here did not answer, so I could not read the refs back."}],
    )
    reviewer = Reviewer()

    await _run(sandbox, scripted, reviewer=reviewer)

    assert reviewer.reviewed == [("review", "The knowledge base named here did not answer, so I could not read the refs back.")]
    assert scripted.calls == ["draft", "review", "present"], "a cannot judge never reruns the drafting step"


async def test_a_brief_whose_source_cannot_be_read_is_refused_and_the_claim_goes(sandbox: AsyncMonty) -> None:
    """A ref the reviewer cannot resolve is a rejection, and the repaired brief no longer leans on it."""
    invented = dict(_EVIDENCE, ref="page:research/paper.one-nobody-has")
    refused = _brief(evidence=[invented, _COUNTEREVIDENCE])
    scripted = Scripted(
        drafts=[refused, _brief()],
        verdicts=[
            {"verdict": "reject", "feedback": "page:research/paper.one-nobody-has resolves to nothing. Drop the claim or cite what you read."},
            {"verdict": "approve", "feedback": ""},
        ],
    )

    result = await _run(sandbox, scripted)

    document = _document(result)
    assert "paper.one-nobody-has" not in document
    assert "paper.long-context-degradation" in document


async def test_a_brief_that_left_the_other_side_out_is_refused_until_it_keeps_it(sandbox: AsyncMonty) -> None:
    """Counterevidence that turned up in the sources has to stand in the brief, in its own place."""
    one_sided = _brief(evidence=[_EVIDENCE])
    scripted = Scripted(
        drafts=[one_sided, _brief()],
        verdicts=[
            {"verdict": "reject", "feedback": "The sources hold a study that gained from ten passages. Keep it and say what it turns on."},
            {"verdict": "approve", "feedback": ""},
        ],
    )

    result = await _run(sandbox, scripted)

    document = _document(result)
    assert "**Arguing the other way**" in document
    assert "Its documents averaged 3,000 tokens" in document, "the condition it turns on is kept, not softened away"


# --- the answers that are not a recommendation --------------------------------------------------


async def test_a_keep_current_brief_is_a_finished_run(sandbox: AsyncMonty) -> None:
    """Concluding that the current course holds is an answer, and the note it proposes says the question is answered."""
    scripted = Scripted(drafts=[_brief()], verdicts=[{"verdict": "approve", "feedback": ""}])

    result = await _run(sandbox, scripted)

    rendered = _rendered(result)
    note = rendered["note_draft"]
    assert isinstance(note, dict)
    assert note["frontmatter"] == {"status": "synthesized", "question_status": "answered"}
    assert note["page_type"] == "question"
    assert "**Decision.** Keep the current course" in _document(result)


async def test_an_insufficient_evidence_brief_may_carry_no_evidence_and_leaves_the_question_open(sandbox: AsyncMonty) -> None:
    """The one shape allowed an empty evidence list, and the only one whose note stays open."""
    unsettled = _brief(
        decision="insufficient_evidence",
        summary="Nothing read settles this. No source measures a window this size over passages as short as ours.",
        recommendation="Do not change the window on this reading.",
        evidence=[],
        limitations="No source measures a window above five over passages under 500 tokens.",
        next_action="Run the comparison on this project's own questions and record the result.",
    )
    scripted = Scripted(drafts=[unsettled], verdicts=[{"verdict": "approve", "feedback": ""}])

    result = await _run(sandbox, scripted)

    rendered = _rendered(result)
    note = rendered["note_draft"]
    assert isinstance(note, dict)
    assert note["frontmatter"] == {"status": "synthesized", "question_status": "open"}
    document = _document(result)
    assert "Nothing in the knowledge bases read settles this yet." in document
    assert "**Decision.** Not enough evidence yet" in document


async def test_a_reviewer_out_of_budget_hands_the_brief_to_the_person_rather_than_passing_it(sandbox: AsyncMonty) -> None:
    """A reviewer that ran out is not an approval. The stop is what the person is told."""
    scripted = Scripted(drafts=[_brief()], verdicts=[], blow_budget=True)
    reviewer = Reviewer()

    await _run(sandbox, scripted, reviewer=reviewer)

    assert len(reviewer.reviewed) == 1
    gate_id, note = reviewer.reviewed[0]
    assert gate_id == "review"
    assert note.startswith("The reviewer ran out of budget before it could decide.")
    assert "tool_calls_limit of 20" in note
    assert scripted.calls == ["draft", "review", "present"], "the run neither failed nor redrafted"


# --- what a finished run produces, and what it does not ------------------------------------------


async def test_the_run_declares_artifacts_and_no_acceptance_of_its_own(sandbox: AsyncMonty) -> None:
    """Three artifacts a person may select, one gate a judge cleared, and an owner acceptance nobody has given."""
    spec = _spec()
    result = spec.result
    assert result is not None
    assert [artifact.id for artifact in result.artifacts] == ["brief", "retention_draft", "evidence"]
    assert [check.gate_id for check in result.required_gates] == ["review"]
    assert result.required_gates[0].accepted_sources == ("judge",), "a machine reviewer cleared it, which is not a person accepting it"

    scripted = Scripted(drafts=[_brief()], verdicts=[{"verdict": "approve", "feedback": ""}])
    rendered = _rendered(await _run(sandbox, scripted))
    refs = tuple(
        ArtifactRef(
            id=artifact.id,
            label=artifact.label,
            run_id="run-1",
            node_id="present",
            value_digest=reviewed_value_digest(canonical_reviewed_value(rendered[artifact.field] if artifact.field else rendered)),
            media_type=artifact.media_type,
        )
        for artifact in result.artifacts
        if artifact.field is not None
    )
    outcome = TaskOutcome(
        run_id="run-1",
        task_id="decision-brief",
        structural_validity=Assessment(status="pass", reason="The graph validated and the run completed."),
        workflow_design=Assessment(status="unknown", reason="Nothing graded the design of this workflow here."),
        evidence_support=Assessment(status="unknown", reason="Nothing graded the evidence here."),
        task_success=Assessment(status="pass", reason="The judge approved the brief the run produced."),
        artifact_refs=refs,
    )

    reviewed = ReviewedTaskOutcome(outcome=outcome, owner_acceptance=Assessment(status="unknown", reason="Nobody has read it yet."))

    assert len(reviewed.outcome.artifact_refs) == 2, "the run produced values a person can select"
    assert reviewed.owner_acceptance.status == "unknown"
    with pytest.raises(ValueError, match="owner acceptance without any review must stay unknown"):
        ReviewedTaskOutcome(outcome=outcome, owner_acceptance=Assessment(status="pass", reason="The run completed."))


def test_the_workflow_keeps_its_stable_identity() -> None:
    """The name and version a saved library row is found under, and the labels a person reads."""
    spec = _spec()
    document = load_spec_document((RESEARCH / "workflows" / "research-decision-brief.md").read_text(encoding="utf-8"))

    assert (spec.name, spec.version) == ("research-decision-brief", 1)
    assert document.frontmatter["title"] == "Settle one decision from the reading"
    presentation = WorkflowPresentation.model_validate(document.frontmatter["presentation"])
    assert [shown.key for shown in presentation.inputs] == ["question", "knowledge_bases", "project_context", "constraints"]
    assert next(shown.control for shown in presentation.inputs if shown.key == "knowledge_bases") == "knowledge-base"
    assert [action.id for action in presentation.actions] == ["keep_as_question"]
