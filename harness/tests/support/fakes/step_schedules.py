"""A `StepSchedules` answering one scripted outcome, for a test of what the failed-fire notice says."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence

    from harness.domain.contracts import FailedFireSteps


@dataclass
class FakeStepSchedules:
    """`StepSchedules` recording every run it is asked about and answering `answer`, or raising `failure` when set."""

    answer: FailedFireSteps | None = None
    failure: Exception | None = None
    asked: list[RunEvidence] = field(default_factory=list)

    async def put_back_steps(self, evidence: RunEvidence) -> FailedFireSteps | None:
        self.asked.append(evidence)
        if self.failure is not None:
            raise self.failure
        return self.answer
