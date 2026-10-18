"""The baseline arm and the grader a benchmark runs on a model, and the text that identifies each."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import DiagnosticObservation, EvaluationError, TaskGrade, measured_usage
from pydantic import BaseModel
from pydantic_ai import Agent

from harness.features.evals.domain.pilot import WorkflowAutomationExecution

if TYPE_CHECKING:
    from core.ai.evaluation.contracts import EvaluationInput, RunIdentity, TaskCase

    from harness.features.evals.domain.benchmark import UsageSource
    from harness.features.evals.internal_contracts import BenchmarkModel

BASELINE_INSTRUCTIONS = (
    "Answer the request directly, in one reply. You have no tools, no workflow and no second pass. "
    "Say what you would produce and what a finished version of it would leave the person holding."
)
"""What the simple baseline is told. It is the work with no automation around it."""

GRADER_INSTRUCTIONS = (
    "You are given one success criterion and one answer. Decide whether the answer meets the criterion. "
    "Judge only what the answer says. You are told nothing about where it came from, and there is nothing else to weigh."
)
"""What the grader is told. It sees a criterion and an answer, never which arm produced either."""

BASELINE_IDENTITY = f"harness.features.evals.outcome_runner.SingleRunTaskRunner\n{BASELINE_INSTRUCTIONS}"
"""What identifies the simple baseline: the runner itself, and what it is told.

A benchmark's stated inputs name this exact content, so a baseline whose instructions changed
fingerprints differently and the comparison is refused as drift.
"""

GRADER_IDENTITY = f"harness.features.evals.outcome_runner.ModelTaskGrader\n{GRADER_INSTRUCTIONS}"
"""What identifies the grader: the grader itself, and what it is told. The same rule as the baseline."""


class GraderVerdict(BaseModel):
    """What the grading model is asked to answer with: the decision, and the reason for it."""

    success: bool
    reason: str


class SingleRunTaskRunner:
    """The simple baseline: one model request on the request itself, no workflow and no tools.

    Each slot gets a run id of its own, derived from the case and the repetition, so the baseline's
    answers are addressable beside the workflow's archived runs. Its agent is built from the declared
    `BenchmarkModel` itself, so what the benchmark classified and what this asks are the same thing.
    """

    def __init__(self, model: BenchmarkModel, *, instructions: str = BASELINE_INSTRUCTIONS) -> None:
        """Build the baseline's agent on `model`.

        Args:
            model: The declared model the baseline asks through.
            instructions: What the baseline is told.
        """
        self.declared = model
        self.agent = Agent(model.model, instructions=instructions)

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """The one declared path this baseline asks through."""
        return (self.declared,)

    @property
    def usage_source(self) -> UsageSource:
        """This record. Every request this baseline makes is admitted here before it is made."""
        return "this_record"

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """One reply to this case's request, with what that one request measured.

        Args:
            case: The case's id and request.
            identity: The run identity of the slot. It is not read.
            repetition: Which repetition of the case this is, part of the run id.

        Returns:
            The reply, its measured usage and the run id it is filed under.

        Raises:
            EvaluationError: the model request failed or the record refused it.
        """
        del identity
        run_id = f"baseline-{case.case_id}-{repetition}"
        try:
            answered = await self.agent.run(case.input)
        except EvaluationError:
            raise
        except Exception as error:
            msg = f"the baseline request for {case.case_id!r} failed: {error}"
            raise EvaluationError(msg) from error
        return WorkflowAutomationExecution(
            output=answered.output,
            usage=measured_usage(answered.usage),
            diagnostics=(DiagnosticObservation(code="execution_outcome", detail="single_run_baseline"),),
            run_id=run_id,
            trial_id=run_id,
        )


class ModelTaskGrader:
    """A `TaskGrader` that asks a model whether one answer meets one case's own success criterion.

    It is handed the criterion and the answer and nothing else: not the variant, not the execution
    diagnostics, not the knowledge base. Its own request is admitted by the record like any other,
    under the grader role, so what grading cost is inside the trial's figures rather than beside them.
    """

    def __init__(self, model: BenchmarkModel, *, instructions: str = GRADER_INSTRUCTIONS) -> None:
        """Build the grader's agent on `model`.

        Args:
            model: The declared model the grader asks through.
            instructions: What the grader is told.
        """
        self.declared = model
        self.agent = Agent(model.model, instructions=instructions, output_type=GraderVerdict)

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """The one declared path this grader asks through."""
        return (self.declared,)

    @property
    def usage_source(self) -> UsageSource:
        """This record. Every request this grader makes is admitted here before it is made."""
        return "this_record"

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Judge `output` against this case's criterion.

        Args:
            case: The case, whose success criterion is the only part the grader sees.
            output: The answer to judge.

        Returns:
            The grade, its reason and what the grading request measured.

        Raises:
            EvaluationError: the grading request failed or the record refused it.
        """
        prompt = f"Success criterion:\n{case.success_criterion}\n\nAnswer:\n{output}"
        try:
            judged = await self.agent.run(prompt)
        except EvaluationError:
            raise
        except Exception as error:
            msg = f"grading {case.case_id!r} failed: {error}"
            raise EvaluationError(msg) from error
        return TaskGrade(
            success=judged.output.success,
            reason=judged.output.reason or "the grader gave no reason",
            usage=measured_usage(judged.usage),
        )
