"""A `CaseRunner` that runs no case, for a test of what step maintenance does with case reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import step_content_address

from harness.domain.contracts import CaseReport, cases_address

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import InlineStep
    from pydantic import JsonValue

    from harness.adapters.contracts import StandInCalls
    from harness.features.maintenance.internal_contracts import CaseRunner


@dataclass
class FakeCaseRunner:
    """Answers each step's case report from `reports` by step id, or a report saying the step has no cases.

    `refusals` holds the refusal a step id's case file gets. `failure` is raised by every `perform` when set.
    `performed` keeps each step id performed and `lists` each stand-in list handed with it, in order.
    """

    refusals: dict[str, str] = field(default_factory=dict)
    reports: dict[str, CaseReport] = field(default_factory=dict)
    failure: Exception | None = None
    performed: list[str] = field(default_factory=list)
    lists: list[StandInCalls] = field(default_factory=list)

    def refusal(self, step_id: str, cases: Mapping[str, JsonValue]) -> str | None:
        """The refusal `refusals` holds for `step_id`, or None."""
        del cases
        return self.refusals.get(step_id)

    async def perform(self, step: InlineStep, step_id: str, cases: Mapping[str, JsonValue] | None, calls: StandInCalls) -> CaseReport:
        """The report `reports` holds for `step_id`, or one saying the step has no cases, raising `failure` first when set."""
        self.performed.append(step_id)
        self.lists.append(calls)
        if self.failure is not None:
            raise self.failure
        kept = self.reports.get(step_id)
        if kept is not None:
            return kept
        return CaseReport(
            step_id=step_id,
            step_hash=step_content_address(step),
            cases_hash=cases_address(cases),
            results=(),
            blocked_by=None,
            no_cases=True,
            stand_in_calls=0,
            refused=None,
        )


_proof: type[CaseRunner] = FakeCaseRunner
"""Pyright fails this module the moment `FakeCaseRunner` drifts from the `CaseRunner` port."""
