"""What a case expects of an answer, and the graded verdict a blind grader returns over it.

An outcome-evaluation case keeps its `AnswerCriterion` away from execution. Only the grader ever
sees it. The criterion says what a correct answer must conclude, what it must not conclude, which
conditions it must carry, which delivered sources must back which claim, and what an acceptable
refusal to answer must say.

The graded verdict is five independent dimensions plus the claims the answer made. Which dimensions
a criterion actually asks for is fixed by the criterion itself, never by the answer:

| Dimension | Graded when |
|---|---|
| `correctness` | the criterion's answerability is not `unsupported` |
| `support` | always |
| `coverage` | the criterion names at least one required condition |
| `contradiction_handling` | at least one evidence requirement carries the `counterevidence` role |
| `abstention` | answerability is `unsupported`, or the criterion carries an abstention requirement |

Every other dimension reads `not_applicable`, which is the only place that status is legal. A
dimension the criterion does ask for is `pass`, `fail` or `unknown`, never `not_applicable`.

`EvidenceRequirement.source_version` pins a claim's support to one version of its source. Left
unset, support is graded from the retained payload alone. Set, the retained `DeliveredSource` for
that reference must carry the same version and a content hash, or the claim it backs is `unknown`
rather than graded against a source whose integrity at delivery time was never observed.
"""

from __future__ import annotations

from typing import Literal

from core.ai.evaluation.contracts import Assessment, DeliveredEvidence, DeliveredEvidenceRef, EvaluationModel, Nonempty, ResourceUsage

type AssessmentStatus = Literal["pass", "fail", "unknown", "not_applicable"]
"""The four verdicts one `Assessment` may carry, named so a caller can hold one in a variable."""

type AnswerDimension = Literal["correctness", "support", "coverage", "contradiction_handling", "abstention"]
"""One graded dimension of an answer. Which of them a criterion asks for is fixed by the criterion."""

ANSWER_DIMENSIONS: tuple[AnswerDimension, ...] = ("correctness", "support", "coverage", "contradiction_handling", "abstention")
"""Every dimension, in the order a verdict reports them."""


class EvidenceRequirement(EvaluationModel):
    """One source a case expects to back or to contradict a claim, and how firmly.

    `role` says which way the source cuts: `support` backs `claim`, `counterevidence` cuts against
    it and an answer that ignores it fails contradiction handling. `required` marks a requirement
    whose claim decides the case rather than merely informing it.

    `source_version` pins the claim to one version of the source. It stays `None` for a case whose
    support does not depend on which version was read.
    """

    source_ref: Nonempty
    role: Literal["support", "counterevidence"]
    claim: Nonempty
    required: bool = True
    source_version: Nonempty | None = None


class AnswerCriterion(EvaluationModel):
    """What a case expects of an answer, held back from execution and read only by the grader.

    `answerability` says what the knowledge base can support at all: `answerable` has a supported
    conclusion, `ambiguous` has more than one reading, `unsupported` has none and the answer must
    say so. `abstention_requirement` says what an acceptable refusal must actually tell the reader.
    """

    answerability: Literal["answerable", "ambiguous", "unsupported"]
    required_conclusions: tuple[Nonempty, ...] = ()
    forbidden_conclusions: tuple[Nonempty, ...] = ()
    required_conditions: tuple[Nonempty, ...] = ()
    evidence: tuple[EvidenceRequirement, ...] = ()
    abstention_requirement: str | None = None

    @property
    def required_dimensions(self) -> frozenset[AnswerDimension]:
        """The dimensions a verdict must grade, per the module's applicability table. Every other dimension is `not_applicable`."""
        required: set[AnswerDimension] = {"support"}
        if self.answerability != "unsupported":
            required.add("correctness")
        if self.required_conditions:
            required.add("coverage")
        if any(requirement.role == "counterevidence" for requirement in self.evidence):
            required.add("contradiction_handling")
        if self.answerability == "unsupported" or self.abstention_requirement is not None:
            required.add("abstention")
        return frozenset(required)


class ClaimAssessment(EvaluationModel):
    """One claim an answer made, whether the retained evidence backs it, and what it cited.

    `evidence` names the delivered payloads the claim rests on. Every one of them must be a payload
    this trial actually retained. `critical` marks a claim whose support decides the outcome.
    """

    claim: Nonempty
    support: Assessment
    evidence: tuple[DeliveredEvidenceRef, ...] = ()
    critical: bool = False


class AnswerAssessment(EvaluationModel):
    """One answer's graded verdict: five independent dimensions, its claims, and what grading cost.

    `overall` folds the rest: `fail` when any graded dimension or critical claim fails, `pass` only
    when every graded dimension and critical claim passes, `unknown` otherwise. A dimension the
    criterion does not ask for is `not_applicable` and takes no part in the fold.

    `usage` is what the grading model spent, kept even when grading could not finish, so an unknown
    verdict never reads as a free one.
    """

    overall: Assessment
    correctness: Assessment
    support: Assessment
    coverage: Assessment
    contradiction_handling: Assessment
    abstention: Assessment
    claims: tuple[ClaimAssessment, ...] = ()
    usage: ResourceUsage = ResourceUsage()

    def dimension(self, name: AnswerDimension) -> Assessment:
        """The assessment `name` names.

        Args:
            name: The dimension to read.

        Returns:
            That dimension's own assessment.
        """
        return {
            "correctness": self.correctness,
            "support": self.support,
            "coverage": self.coverage,
            "contradiction_handling": self.contradiction_handling,
            "abstention": self.abstention,
        }[name]


class AnswerGradeDeps(EvaluationModel):
    """Everything the grading model is given, and the whole of it.

    The criterion, the answer under grading and the exact payloads the trial retained. No variant,
    no discovery score, no case metadata and no tools, so a verdict cannot be reached by knowing
    which arm produced the answer or by reading a source as it stands now.
    """

    criterion: AnswerCriterion
    answer: str
    evidence: tuple[DeliveredEvidence, ...]

