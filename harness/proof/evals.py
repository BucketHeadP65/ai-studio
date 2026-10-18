"""The referee, restated in pydantic-evals' own vocabulary -- their nouns, our arithmetic.

`proof/groundtruth.py` already holds the whole metric: a `ResearchQuestion` knows which pages
its published synthesis cites, and `recall` is set arithmetic over those ids. This module adds
no metric. It restates the same referee as the objects the ecosystem reads -- a `Dataset` of
`Case`s and an `Evaluator` -- so this experiment can be run, reported and argued about with the
library evaluation is native to, and not only through this harness's own report.

The ruling this module exists to make legible: **the referee speaks the ecosystem's own
vocabulary; the metric stays arithmetic -- no LLMJudge anywhere near trust.** `pydantic_evals`
ships `LLMJudge` and `GEval`, and both are the wrong instrument here. A score that decides
whether a stored artifact keeps its standing has to be a number nothing can be talked into, so
`EvidenceRecall` calls `ResearchQuestion.recall` and reports what it says. There is exactly one
implementation of this metric in the repository, and it is not in this file.

Two properties are load-bearing, and both hold by construction:

* **Import-light.** Nothing here reaches for settings, a database, a model or a credential.
  `evidence_dataset()` reads the exported bundle and nothing else, so the dataset can be built,
  inspected and serialised on a machine with no knowledge base at all. The live runners
  (`proof.baseline`, `proof.reuse`) are deliberately *not* imported: they touch `harness.settings`
  at module scope, and a dataset you cannot construct without credentials is not a dataset.
* **One source of arithmetic.** `EvidenceRecall` computes nothing of its own. It delegates
  recall to `ResearchQuestion.recall` and mirrors `records.build_record`'s contamination rule,
  so a pydantic-evals run and the harness's own `RunRecord` can never report different numbers
  for the same run -- which is the only reason it is safe to have two readers of one metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EvaluationReason, Evaluator

from proof.groundtruth import ResearchQuestion, load_questions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from pydantic_evals.evaluators import EvaluatorContext

    from proof.records import RunRecord

WITHHELD_PREFIX = "q."
"""How a withheld question page's id begins, and therefore how a leak is spotted.

Mirrors `records.build_record`'s contamination check verbatim -- `any(page.startswith("q.")
for page in surfaced)` -- rather than importing it, because that check lives inline in the one
record assembler and this module changes nothing there. `tests/unit/test_proof_evals.py` pins
the two together against a real `build_record` call, so the mirror cannot drift silently.
"""


class SurfacedEvidence(BaseModel):
    """What a run surfaced, as the evaluator reads it -- this dataset's task-output shape.

    Deliberately narrower than `records.RunRecord`: recall is a function of the ids a run
    touched and of nothing else, so any producer of those ids -- a replay of a recorded
    experiment, a live runner, a hand-written fixture -- is scoreable without inventing token
    counts or a wall clock it never had.

    Attributes:
        surfaced: Knowledge base ids the run's tool calls returned. Section ids (`paper.x#section`) may
            be passed as-is; `ResearchQuestion.recall` folds them to their page.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surfaced: tuple[str, ...] = ()


def surfaced_from(record: RunRecord) -> SurfacedEvidence:
    """The evaluator's view of a recorded run: the ids it touched, and nothing else."""
    return SurfacedEvidence(surfaced=record.surfaced)


@dataclass(repr=False)
class EvidenceRecall(Evaluator[str, SurfacedEvidence, ResearchQuestion]):
    """Score a run on the fraction of its question's cited evidence it actually surfaced.

    Deterministic set arithmetic, delegated whole to `ResearchQuestion.recall`. No model is
    consulted, no prose is read, and `LLMJudge` is deliberately not used: a score that decides
    whether a stored artifact keeps its standing must be one nothing can argue with.

    Returns a mapping, so one evaluator reports both halves of the ruling:

    * `evidence_recall` -- a float in `[0.0, 1.0]`, `ResearchQuestion.recall`'s own answer, with
      the evidence the run never reached named in the reason.
    * `uncontaminated` -- a bool, and therefore an *assertion* in pydantic-evals' vocabulary,
      which is why it is stated positively: an assertion is a thing that ought to hold. It fails
      when a withheld question page leaked into the surfaced set, and a contaminated run scores
      `0.0` -- disqualified exactly as `report.render_report` disqualifies it, and, exactly as
      there, said out loud rather than quietly averaged away.
    """

    def evaluate(self, ctx: EvaluatorContext[str, SurfacedEvidence, ResearchQuestion]) -> Mapping[str, EvaluationReason]:
        """Score one case against its question's citations, and report why it scored that.

        Raises:
            ValueError: If the case carries no `ResearchQuestion` metadata. Without the
                groundtruth there is no arithmetic to delegate to, and inventing a zero for a
                case nobody supplied an answer key for would be a fabricated verdict.
        """
        question = ctx.metadata
        if question is None:
            msg = f"case {ctx.name!r} carries no ResearchQuestion, so there is no groundtruth to score it against"
            raise ValueError(msg)
        recall = question.recall(ctx.output.surfaced)
        leaked = ", ".join(sorted(page for page in ctx.output.surfaced if page.startswith(WITHHELD_PREFIX)))
        if leaked:
            return {
                "evidence_recall": EvaluationReason(
                    value=0.0, reason=f"disqualified by contamination ({leaked}); the arithmetic alone would have read {recall:.2f}"
                ),
                "uncontaminated": EvaluationReason(value=False, reason=f"withheld question page(s) {leaked} reached this run"),
            }
        missed = ", ".join(sorted(question.missed(ctx.output.surfaced)))
        return {
            "evidence_recall": EvaluationReason(value=recall, reason=f"missed: {missed}" if missed else "every cited source was surfaced"),
            "uncontaminated": EvaluationReason(value=True, reason="no withheld question page reached this run"),
        }


def evidence_dataset(questions: Sequence[ResearchQuestion] | None = None) -> Dataset[str, SurfacedEvidence, ResearchQuestion]:
    """The knowledge base's own questions as a pydantic-evals `Dataset`, scored by `EvidenceRecall`.

    One `Case` per question page: named by the question's knowledge base id, given the question text as
    the task input -- the same prompt both experiment paths receive -- carrying the
    `ResearchQuestion` itself as metadata so the evaluator scores through the groundtruth's own
    arithmetic, and declaring as expected output the cited-id set, which is what a run that
    reached every source the published synthesis rests on would have surfaced.

    Args:
        questions: The questions to build cases from. Defaults to `load_questions()` -- the six
            `question` pages of the exported research bundle. The parameter is the seam that
            lets a dataset be built from a fixed set (a subset, a test, an older bundle) with no
            export on disk, which is what keeps this module constructible with nothing running.
    """
    asked = load_questions() if questions is None else questions
    return Dataset[str, SurfacedEvidence, ResearchQuestion](
        name="evidence-recall",
        cases=[
            Case(
                name=question.id,
                inputs=question.question,
                metadata=question,
                expected_output=SurfacedEvidence(surfaced=tuple(sorted(question.sources))),
            )
            for question in asked
        ],
        evaluators=[EvidenceRecall()],
    )


def replay(
    records: Iterable[RunRecord], *, path: Literal["baseline", "reuse"], questions: Sequence[ResearchQuestion] | None = None
) -> Callable[[str], SurfacedEvidence]:
    """A task function that answers each case from an experiment already on disk.

    `Dataset.evaluate` wants a callable from a case's input to the task's output; for one path
    of a finished experiment, that callable is a lookup rather than a run. This is what makes
    the dataset scoreable with nothing running -- no knowledge base, no model, no store -- and it is
    why the live runners are not imported here.

    Args:
        records: Run records to answer from. A directory holding both paths is fine.
        path: Which experiment path to replay. Required rather than inferred: `proof/out/`
            holds both, and folding them into one lookup would compare a path against itself.
        questions: The questions whose text keys the lookup. Defaults to `load_questions()`;
            pass whatever sequence `evidence_dataset` was built from.

    Returns:
        A callable from a question's text to what that path's recorded run surfaced. It raises
        `LookupError` for a question that path never ran: an absent run is a gap in the
        experiment, and reporting it as a zero would put a score on something nobody measured.
    """
    asked = load_questions() if questions is None else questions
    ids_by_text = {question.question: question.id for question in asked}
    records_by_id = {record.question_id: record for record in records if record.path == path}

    def surfaced(question: str) -> SurfacedEvidence:
        record = records_by_id.get(ids_by_text.get(question, ""))
        if record is None:
            msg = f"no {path} record for question {question!r}; an unrun question is a gap in the experiment, not a zero"
            raise LookupError(msg)
        return surfaced_from(record)

    return surfaced
