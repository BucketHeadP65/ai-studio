from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import (
    EvaluationError,
    EvaluationInput,
    Provenance,
    ResourceUsage,
    RunIdentity,
    TaskCase,
    TaskExecution,
    TaskGrade,
    content_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

PROVENANCE = Provenance(source="fixture:agent", kind="synthetic", review="agent", reviewer="test-author")


def identity(variant: str) -> RunIdentity:
    return RunIdentity(
        model="test:model",
        model_version="1",
        prompt_fingerprint=content_fingerprint(variant),
        implementation_fingerprint=content_fingerprint(variant),
        conditions_fingerprint=content_fingerprint("same settings and environment"),
        grader_fingerprint=content_fingerprint("independent equality grader v1"),
    )


def task_case(case_id: str, split: str = "test") -> TaskCase:
    return TaskCase.model_validate(
        {"case_id": case_id, "input": f"task {case_id}", "success_criterion": "success", "split": split, "provenance": PROVENANCE}
    )


class FakeTaskRunner:
    def __init__(
        self,
        outputs: Mapping[tuple[str, str], str | EvaluationError],
        baseline_cost: float | None = 0.1,
        candidate_cost: float | None = 0.1,
    ) -> None:
        self.outputs = dict(outputs)
        self.baseline_cost = baseline_cost
        self.candidate_cost = candidate_cost

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> TaskExecution:
        variant = "baseline" if identity.implementation_fingerprint == content_fingerprint("baseline") else "candidate"
        output = self.outputs.get((variant, case.case_id), "failure")
        if isinstance(output, EvaluationError):
            raise output
        return TaskExecution(
            output=output,
            usage=ResourceUsage(
                input_tokens=10, output_tokens=5, requests=1, cost_usd=self.baseline_cost if variant == "baseline" else self.candidate_cost
            ),
        )


class FakeTaskGrader:
    def __init__(self, usage: ResourceUsage | None = None, failure: EvaluationError | None = None) -> None:
        self.usage = usage or ResourceUsage(cost_usd=0)
        self.failure = failure

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        if self.failure is not None:
            raise self.failure
        return TaskGrade(success=output == case.success_criterion, reason="Independent task output equality", usage=self.usage)
