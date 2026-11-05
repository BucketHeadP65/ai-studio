"""Run a reusable step's own cases once per content address, through the registry's own executor, and keep the report.

Run them through a registry built with stand-ins, so every write the step makes is checked and recorded and nothing
lands in a knowledge base.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING, get_type_hints

from core.ai.workflows.contracts import AgentStep, Briefing, Program, Shape, build_type, step_content_address
from pydantic_evals.evaluators import LLMJudge

from harness.domain.contracts import CASE_RUN_TOOLS, CaseReport
from harness.features.evals.domain.step_evaluation import CaseDataset, OutcomeMatches, case_result
from harness.features.evals.exceptions import CaseRunRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from core.ai.workflows.contracts import InlineStep, StepRegistry
    from pydantic import JsonValue

    from harness.adapters.contracts import StandInCalls
    from harness.features.evals.internal_contracts import CaseReports

_VALUE = "value"
"""The one field of the object a non-object output shape is wrapped in, so `build_type` builds its type."""


async def perform_step_cases(  # noqa: PLR0913, PLR0917 (each argument is a separate term the caller sets)
    step: InlineStep,
    step_id: str,
    cases: Mapping[str, JsonValue] | None,
    registry: StepRegistry,
    reports: CaseReports,
    calls: StandInCalls,
) -> CaseReport:
    """The case report for `step` and `cases`: the kept one, or a new one from running every case.

    A briefing's cases run through `registry.perform` and a program's through `registry.run_code`, one case at a time.
    Build `registry` with the same `calls` as its stand-ins, so every write the cases make is counted. A new report is
    kept through `reports`, so the same step and cases never run twice, unless a case errored: that report is returned
    and not kept, so the cases run again next time. Nothing runs, and nothing is kept, for a step with no cases, an
    agent step, a step whose output is another step's type, or a step holding a tool outside `CASE_RUN_TOOLS`, which
    the report names in `blocked_by`.

    Args:
        step: The step whose cases run.
        step_id: The step's id in the library, which the report names.
        cases: The step's dataset in `pydantic_evals` form, or `None` when it has none.
        registry: The registry the cases run through.
        reports: Where case reports are kept.
        calls: The list the registry's stand-ins record into.

    Returns:
        The case report.

    Raises:
        CaseRunRefusedError: `registry` cannot perform a briefing or run a program, or an `LLMJudge` in the dataset
            names no model. Nothing has run.
    """
    report = CaseReport(
        step_id=step_id,
        step_hash=step_content_address(step),
        cases_hash=_cases_address(cases),
        results=(),
        blocked_by=None,
        no_cases=True,
        stand_in_calls=0,
    )
    if cases is None or not cases.get("cases"):
        return report
    if (kept := reports.get(report.step_hash, report.cases_hash)) is not None:
        return kept
    if isinstance(step, AgentStep):
        return report
    if (blocked := next((tool for tool in step.tools if tool not in CASE_RUN_TOOLS), None)) is not None:
        return report.model_copy(update={"no_cases": False, "blocked_by": blocked})
    output_type = _output_type(step)
    if output_type is None:
        return report

    task = _case_task(step, step_id, output_type, registry)
    dataset = CaseDataset.from_dict(dict(cases), custom_evaluator_types=[OutcomeMatches], default_name=step.name)
    _refuse_unnamed_judges(dataset, step_id)
    before = len(calls)
    evaluation = await dataset.evaluate(task, max_concurrency=1, progress=False)
    by_name = {result.name: result for result in (case_result(case) for case in (*evaluation.cases, *evaluation.failures))}
    report = report.model_copy(
        update={"results": tuple(by_name[case.name] for case in dataset.cases if case.name in by_name), "no_cases": False, "stand_in_calls": len(calls) - before}
    )
    if not any(result.errored for result in report.results):
        reports.put(report)
    return report


def _refuse_unnamed_judges(dataset: CaseDataset, step_id: str) -> None:
    """Refuse a dataset holding an `LLMJudge` that names no model, which would grade on pydantic-evals' own default."""
    judged = [("every case", dataset.evaluators), *((f"case {case.name!r}", case.evaluators) for case in dataset.cases)]
    if unnamed := [where for where, evaluators in judged if any(isinstance(each, LLMJudge) and each.model is None for each in evaluators)]:
        msg = (
            f"the cases of {step_id} cannot run: the LLMJudge for {', '.join(unnamed)} names no model, so it would grade on a "
            f"model outside this deployment. Give it a `model`: name a model this deployment's tiers can serve."
        )
        raise CaseRunRefusedError(msg)


def _case_task(step: Briefing | Program, step_id: str, output_type: type, registry: StepRegistry) -> Callable[[dict[str, JsonValue]], Awaitable[object]]:
    """One case's run of `step` through the registry, refused before any case runs when the registry cannot run it."""
    if isinstance(step, Briefing):
        if (perform := registry.perform) is None:
            msg = f"the cases of {step_id} cannot run: this registry performs no briefing"
            raise CaseRunRefusedError(msg)

        async def performed(inputs: dict[str, JsonValue]) -> object:
            return await perform(step, output_type, **inputs)

        return performed
    if (run_code := registry.run_code) is None:
        msg = f"the cases of {step_id} cannot run: this registry has no sandbox to run a program in"
        raise CaseRunRefusedError(msg)

    async def ran(inputs: dict[str, JsonValue]) -> object:
        return await run_code(step, output_type, **inputs)

    return ran


def _cases_address(cases: Mapping[str, JsonValue] | None) -> str:
    """The SHA-256 of the dataset's canonical JSON, so the same cases written two ways share one address."""
    return hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _output_type(step: Briefing | Program) -> type | None:
    """The type the step's own output shape builds, or `None` when its output is another step's type."""
    if not isinstance(step.output, Shape):
        return None
    schema = step.output.json_schema
    where = f"{step.name}.output"
    if schema.get("type") == "object":
        return build_type(schema, name=step.name, where=where)
    wrapped = build_type({"type": "object", "properties": {_VALUE: schema}, "required": [_VALUE]}, name=step.name, where=where)
    return get_type_hints(wrapped)[_VALUE]
