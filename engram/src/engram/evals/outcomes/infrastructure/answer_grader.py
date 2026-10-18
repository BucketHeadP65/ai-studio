"""Grading an answer from the evidence the trial retained, with no tools and no live source.

The grader holds no retrieval tools, never learns which arm produced the answer, and reads every
payload back through a `DeliveredEvidenceReader` in ordinal order before the model sees anything.
A reference the reader refuses makes every required dimension and the overall verdict `unknown`,
and no request is made.
Requirements and claims pair one to one, in declared order, on claim text with whitespace collapsed
and case folded, and an unanswered requirement is recorded as an `unknown` claim that is `critical`
only when the requirement is `required`.
Any claim, paired or not, is `unknown` when it cites a reference the trial did not retain (by evidence
id and payload hash). A paired claim is also `unknown` when its requirement pins a `source_version` the retained source does not carry with a content
hash, or when it is marked supported without citing the requirement's own `source_ref`.
A dimension outside `AnswerCriterion.required_dimensions` is set to `not_applicable` and a required
one the model skipped becomes `unknown`, and a model that times out or returns output the type refuses
folds the same way, keeping the usage it spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import (
    Assessment,
    DeliveredEvidence,
    DeliveredEvidenceError,
    DeliveredEvidenceReader,
    DeliveredEvidenceRef,
    ResourceUsage,
    measured_usage,
)
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError

from engram.evals.outcomes.domain.answers import (
    ANSWER_DIMENSIONS,
    AnswerAssessment,
    AnswerCriterion,
    AnswerDimension,
    AnswerGradeDeps,
    ClaimAssessment,
    EvidenceRequirement,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models import KnownModelName, Model

ANSWER_GRADER_INSTRUCTIONS = """\
You grade one answer against the criterion it was written for.

You have no tools and no access to any source. The delivered evidence in the request is the whole
record: it is the exact text the answering agent was handed, byte for byte. Grade the answer against
that text and the criterion, and nothing else. Never assume a source says more than the retained
payload shows.

Grade each dimension independently:

- correctness: does the answer reach the criterion's required conclusions and avoid its forbidden
  ones?
- support: does the retained evidence actually back what the answer claims? An answer whose
  conclusion the evidence contradicts fails here even when it cites the right sources.
- coverage: does the answer carry every condition the criterion requires? An unconditional answer to
  evidence that holds only under a condition fails here.
- contradiction_handling: does the answer address the counterevidence the record delivered? Ignoring
  delivered counterevidence fails here.
- abstention: when the criterion says the knowledge base cannot support an answer, does the answer
  say so and explain why? A recommendation in place of that refusal fails here.

Use `unknown` when the retained evidence does not let you decide. Do not use `not_applicable`.

`claims` must hold one assessment for every entry of the criterion's `evidence`, with that entry's
`claim` text copied into `claim` exactly as it is written there. Say whether the retained evidence
backs it (`pass`), cuts against it (`fail`) or leaves it undecided (`unknown`). A requirement you
return no assessment for counts as undecided, never as met. Add a further assessment, in your own
words, for any other substantive claim the answer made.

For each claim, cite the delivered evidence references that decided it, exactly as they appear in the
request. Only cite a reference that is in the request. Mark a claim `critical` when the answer's
usefulness rests on it.

Every `reason` is one or two plain sentences saying what in the evidence decided it.
"""


def build_answer_grader_agent(model: Model | KnownModelName | str) -> Agent[AnswerGradeDeps, AnswerAssessment]:
    """Build the grading agent: structured verdict, no tools, no retrieval.

    Args:
        model: The grading model, a network-free `TestModel` or `FunctionModel` in tests.

    Returns:
        An agent whose only tool is the `AnswerAssessment` output tool.
    """
    return Agent(model, deps_type=AnswerGradeDeps, output_type=AnswerAssessment, instructions=ANSWER_GRADER_INSTRUCTIONS)


def answer_grade_request(deps: AnswerGradeDeps) -> str:
    """The one request the grading model receives, carrying nothing beyond `deps`.

    Args:
        deps: The criterion, the answer under grading and the retained payloads.

    Returns:
        The request text: the criterion, the answer, and every payload verbatim in ordinal order.
    """
    return "\n\n".join(
        (
            "# Criterion",
            deps.criterion.model_dump_json(indent=2),
            "# Answer under grading",
            deps.answer,
            "# Delivered evidence, exactly as the answering agent received it",
            _evidence_block(deps.evidence),
        )
    )


def _evidence_block(evidence: tuple[DeliveredEvidence, ...]) -> str:
    """Every retained payload under the reference that names it, verbatim and in ordinal order."""
    if not evidence:
        return "This trial retained no delivered evidence."
    return "\n\n".join(
        f"## evidence {item.ref.evidence_id} (ordinal {item.ref.ordinal}, sources: {_source_list(item.ref)})\n{item.payload_json}"
        for item in evidence
    )


def _source_list(ref: DeliveredEvidenceRef) -> str:
    """The references a payload carried, with the version each one was observed at."""
    if not ref.sources:
        return "none recorded"
    return ", ".join(
        source.source_ref if source.source_version is None else f"{source.source_ref}@{source.source_version}" for source in ref.sources
    )


_NOTHING_SPENT = ResourceUsage()
"""What a grading attempt that never reached the model cost."""

_WHY_NOT_APPLICABLE: Mapping[AnswerDimension, str] = {
    "correctness": "the knowledge base supports no conclusion for this question, so there is none to be correct about",
    "support": "support is graded for every criterion",
    "coverage": "the criterion requires no condition to be carried",
    "contradiction_handling": "the criterion names no counterevidence requirement",
    "abstention": "the question is answerable and the criterion asks for no refusal",
}


@dataclass(frozen=True)
class _GraderRun:
    """What one grading run spent, and why it produced nothing when it produced nothing."""

    usage: ResourceUsage
    reason: str


@dataclass(frozen=True)
class PydanticAnswerGrader:
    """An `AnswerGrader` over a grading agent and the store the trial's evidence was written to.

    One grader serves any number of trials. It holds the reader, never a source, so nothing it
    returns can depend on a knowledge base as it stands now.
    """

    agent: Agent[AnswerGradeDeps, AnswerAssessment]
    reader: DeliveredEvidenceReader

    async def __call__(self, criterion: AnswerCriterion, answer: str, evidence: tuple[DeliveredEvidenceRef, ...]) -> AnswerAssessment:
        """The verdict on `answer`, from `criterion` and the payloads `evidence` names.

        Args:
            criterion: What the case expects of the answer.
            answer: The answer under grading.
            evidence: The delivered-evidence references this trial retained.

        Returns:
            The graded verdict. Evidence the reader refuses, a grading model that fails and a
            verdict bound to a payload the trial never retained all read `unknown` rather than
            raising.
        """
        refs = tuple(sorted(evidence, key=lambda ref: ref.ordinal))
        try:
            payloads = tuple([await self.reader.read(ref) for ref in refs])
        except DeliveredEvidenceError as error:
            return unreadable_assessment(criterion, str(error))
        deps = AnswerGradeDeps(criterion=criterion, answer=answer, evidence=payloads)
        verdict, run = await self._graded(deps)
        if verdict is None:
            return unreadable_assessment(criterion, run.reason, usage=run.usage)
        return _bound_verdict(verdict, criterion, refs, usage=run.usage)

    async def _graded(self, deps: AnswerGradeDeps) -> tuple[AnswerAssessment | None, _GraderRun]:
        """Run the grading agent once, keeping the usage it spent whether or not it finished."""
        async with self.agent.iter(answer_grade_request(deps), deps=deps) as run:
            try:
                async for _node in run:
                    pass
            except (AgentRunError, TimeoutError) as error:
                return None, _GraderRun(usage=measured_usage(run.usage), reason=f"the grading model did not return a verdict: {error}")
            result = run.result
            if result is None:
                return None, _GraderRun(usage=measured_usage(run.usage), reason="the grading model produced no verdict")
            return result.output, _GraderRun(usage=measured_usage(run.usage), reason="")


def unreadable_assessment(criterion: AnswerCriterion, reason: str, *, usage: ResourceUsage = _NOTHING_SPENT) -> AnswerAssessment:
    """The verdict when grading could not be reached at all: every asked dimension `unknown`.

    Args:
        criterion: The criterion whose asked dimensions go unknown.
        reason: What stopped the grading, stated for a reader of the report.
        usage: What was spent before grading stopped, zero when no request was made.

    Returns:
        A verdict with `unknown` on every dimension the criterion asks for, `not_applicable` on the
        rest, no claims, and `unknown` overall.
    """
    asked = criterion.required_dimensions
    dimensions: dict[AnswerDimension, Assessment] = {name: _unknown_or_not_applicable(name, asked, reason) for name in ANSWER_DIMENSIONS}
    return _assembled(Assessment(status="unknown", reason=reason), dimensions, claims=(), usage=usage)


def _unknown_or_not_applicable(name: AnswerDimension, asked: frozenset[AnswerDimension], reason: str) -> Assessment:
    """`unknown` for a dimension the criterion asks for, `not_applicable` for one it does not."""
    if name in asked:
        return Assessment(status="unknown", reason=reason)
    return Assessment(status="not_applicable", reason=_WHY_NOT_APPLICABLE[name])


def _assembled(
    overall: Assessment, dimensions: Mapping[AnswerDimension, Assessment], *, claims: tuple[ClaimAssessment, ...], usage: ResourceUsage
) -> AnswerAssessment:
    """One verdict from its already-settled parts, each dimension named rather than unpacked."""
    return AnswerAssessment(
        overall=overall,
        correctness=dimensions["correctness"],
        support=dimensions["support"],
        coverage=dimensions["coverage"],
        contradiction_handling=dimensions["contradiction_handling"],
        abstention=dimensions["abstention"],
        claims=claims,
        usage=usage,
    )


def _bound_verdict(
    graded: AnswerAssessment, criterion: AnswerCriterion, refs: tuple[DeliveredEvidenceRef, ...], *, usage: ResourceUsage
) -> AnswerAssessment:
    """The model's verdict bound back to the trial's record, its dimensions and its fold settled."""
    claims = _bound_claims(graded.claims, criterion, refs)
    asked = criterion.required_dimensions
    dimensions: dict[AnswerDimension, Assessment] = {name: _settled_dimension(name, graded.dimension(name), asked) for name in ANSWER_DIMENSIONS}
    return _assembled(_overall(dimensions, claims), dimensions, claims=claims, usage=usage)


def _settled_dimension(name: AnswerDimension, graded: Assessment, asked: frozenset[AnswerDimension]) -> Assessment:
    """One dimension held to the criterion: only an asked dimension is graded, and it must be."""
    if name not in asked:
        return Assessment(status="not_applicable", reason=_WHY_NOT_APPLICABLE[name])
    if graded.status == "not_applicable":
        return Assessment(status="unknown", reason=f"the criterion asks for {name} and the grader did not grade it", grader=graded.grader)
    return graded


def _retained(refs: tuple[DeliveredEvidenceRef, ...]) -> dict[tuple[str, str], DeliveredEvidenceRef]:
    """The trial's own references, keyed by the identity and hash a citation must match."""
    return {(ref.evidence_id, ref.payload_sha256): ref for ref in refs}


def claim_key(claim: str) -> str:
    """One claim's comparison identity: whitespace collapsed, ends trimmed, casefolded.

    A grading model echoes a requirement's claim back as free text, so spacing and capitalization
    alone must not decide whether the requirement was answered.

    Args:
        claim: The claim text to fold.

    Returns:
        The folded comparison identity.
    """
    return " ".join(claim.split()).casefold()


def _bound_requirements(claims: tuple[ClaimAssessment, ...], criterion: AnswerCriterion) -> dict[int, int]:
    """Map each claim's position to the position of the requirement it answers, in declared order, one requirement per claim."""
    answered: dict[int, int] = {}
    for position, requirement in enumerate(criterion.evidence):
        key = claim_key(requirement.claim)
        index = next((slot for slot, claim in enumerate(claims) if slot not in answered and claim_key(claim.claim) == key), None)
        if index is not None:
            answered[index] = position
    return answered


def _bound_claims(
    claims: tuple[ClaimAssessment, ...], criterion: AnswerCriterion, refs: tuple[DeliveredEvidenceRef, ...]
) -> tuple[ClaimAssessment, ...]:
    """Every claim held to the record, plus one per requirement the grader never answered."""
    retained = _retained(refs)
    answered = _bound_requirements(claims, criterion)
    requirement_of = {index: criterion.evidence[position] for index, position in answered.items()}
    bound = tuple(_bound_claim(claim, requirement_of.get(index), retained, refs) for index, claim in enumerate(claims))
    taken = set(answered.values())
    return bound + tuple(
        _unanswered_claim(requirement) for position, requirement in enumerate(criterion.evidence) if position not in taken
    )


def _unanswered_claim(requirement: EvidenceRequirement) -> ClaimAssessment:
    """The `unknown` claim recording a requirement the grader never assessed, critical when the requirement is required."""
    reason = _UNANSWERED if requirement.required else _UNADDRESSED
    return ClaimAssessment(
        claim=requirement.claim,
        support=Assessment(status="unknown", reason=reason.format(requirement.source_ref)),
        critical=requirement.required,
    )


_UNOBSERVED_VERSION = "the criterion pins {0} to version {1} and the retained record carries no matching version and content hash"

_UNANSWERED = "the criterion requires {0} to decide this claim and the grader returned no assessment of it"

_UNADDRESSED = "the criterion names {0} for this claim, the grader returned no assessment of it, and it does not decide the answer"


def _version_observed(requirement: EvidenceRequirement, refs: tuple[DeliveredEvidenceRef, ...]) -> bool:
    """Whether the record observed `requirement`'s pinned version, vacuously true when none is pinned."""
    if requirement.source_version is None:
        return True
    return any(
        source.source_ref == requirement.source_ref
        and source.source_version == requirement.source_version
        and source.source_content_sha256 is not None
        for ref in refs
        for source in ref.sources
    )


def _bound_claim(
    claim: ClaimAssessment,
    requirement: EvidenceRequirement | None,
    retained: Mapping[tuple[str, str], DeliveredEvidenceRef],
    refs: tuple[DeliveredEvidenceRef, ...],
) -> ClaimAssessment:
    """One claim held to the record: an unretained citation, an unobserved version or a bare assertion is unknown."""
    outside = next((ref for ref in claim.evidence if (ref.evidence_id, ref.payload_sha256) not in retained), None)
    if outside is not None:
        return _unsupportable(claim, f"the claim cites {outside.evidence_id}, which this trial did not retain under that hash")
    if requirement is None:
        return claim
    if not _version_observed(requirement, refs):
        return _unsupportable(claim, _UNOBSERVED_VERSION.format(requirement.source_ref, requirement.source_version))
    if claim.support.status == "pass" and not _cites_source(claim, requirement, retained):
        return _unsupportable(claim, f"the claim is graded supported without citing retained evidence carrying {requirement.source_ref}")
    return claim


def _unsupportable(claim: ClaimAssessment, reason: str) -> ClaimAssessment:
    """The same claim with its support unknown, the grader that reached it kept."""
    return claim.model_copy(update={"support": Assessment(status="unknown", reason=reason, grader=claim.support.grader)})


def _cites_source(
    claim: ClaimAssessment, requirement: EvidenceRequirement, retained: Mapping[tuple[str, str], DeliveredEvidenceRef]
) -> bool:
    """Whether the claim's cited payloads actually carried the requirement's own source."""
    return any(
        source.source_ref == requirement.source_ref
        for ref in claim.evidence
        for source in retained[ref.evidence_id, ref.payload_sha256].sources
    )


def _overall(dimensions: Mapping[AnswerDimension, Assessment], claims: tuple[ClaimAssessment, ...]) -> Assessment:
    """Fold the settled dimensions and the critical claims into the one verdict a gate reads."""
    critical = tuple(claim for claim in claims if claim.critical)
    failed: tuple[AnswerDimension, ...] = tuple(name for name in ANSWER_DIMENSIONS if dimensions[name].status == "fail")
    failed_claims = tuple(claim.claim for claim in critical if claim.support.status == "fail")
    if failed or failed_claims:
        return Assessment(status="fail", reason=_fold_reason("failed", failed, failed_claims))
    graded: tuple[AnswerDimension, ...] = tuple(name for name in ANSWER_DIMENSIONS if dimensions[name].status != "not_applicable")
    unknown: tuple[AnswerDimension, ...] = tuple(name for name in graded if dimensions[name].status != "pass")
    unknown_claims = tuple(claim.claim for claim in critical if claim.support.status != "pass")
    if unknown or unknown_claims:
        return Assessment(status="unknown", reason=_fold_reason("could not be decided", unknown, unknown_claims))
    return Assessment(status="pass", reason="every dimension the criterion asks for and every critical claim passed")


def _fold_reason(verb: str, dimensions: tuple[AnswerDimension, ...], claims: tuple[str, ...]) -> str:
    """Name what drove the fold, dimensions first and then the critical claims."""
    parts = tuple(f"{name} {verb}" for name in dimensions) + tuple(f"critical claim {claim!r} {verb}" for claim in claims)
    return ", ".join(parts)
