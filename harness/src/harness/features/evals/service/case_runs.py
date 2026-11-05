"""Run a reusable step's own cases once per content address, through the registry's own executor, and keep the report.

Run them through a registry built with stand-ins, so every write the step makes is checked and recorded and nothing
lands in a knowledge base.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING, get_type_hints

from core.ai.workflows.contracts import AgentStep, Briefing, Program, Shape, StepIdentity, build_type, performing_step, step_content_address
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai_harness.spend import SpendLimitExceeded
from pydantic_core import to_jsonable_python
from pydantic_evals.evaluators import LLMJudge

from harness.domain.contracts import CASE_RUN_TOOLS, CaseReport, CaseResult
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

_OUTSIDE_THE_STEP: tuple[type[BaseException], ...] = (ModelAPIError, TimeoutError, SpendLimitExceeded)
"""The errors whose cause lies outside the step: a model connection or provider error, a timeout, and the deployment's
spend limit. A case one of them stops errored. Every other raise is the step's own failure."""


async def perform_step_cases(  # noqa: PLR0913, PLR0917 (each argument is a separate term the caller sets)
    step: InlineStep,
    step_id: str,
    cases: Mapping[str, JsonValue] | None,
    registry: StepRegistry,
    reports: CaseReports,
    calls: StandInCalls,
) -> CaseReport:
    """The case report for `step` and `cases`: the kept one, or a new one from running every case.

    A briefing's cases run through `registry.perform` and a program's through `registry.run_code`, one case at a time,
    each as a step of its own named `<step_id>#<case name>`, never as the step calling this. A case with no name is
    named `Case <position>`, counted from 1. Build `registry` with the same `calls` as its stand-ins, so every write
    the cases make is counted. A new report is kept through `reports`, so the same step and cases never run twice,
    unless a case errored: that report is returned and not kept, so the cases run again next time. A kept report is
    returned under `step_id`. Nothing runs, and nothing is kept, for a step with no cases, an agent step, a step whose
    output is another step's type, or a step holding a tool outside `CASE_RUN_TOOLS` or outside `registry.grants`,
    which the report names in `blocked_by`.

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
        return kept.model_copy(update={"step_id": step_id})
    if isinstance(step, AgentStep):
        return report
    granted = frozenset(registry.grants)
    if (blocked := next((tool for tool in step.tools if tool not in CASE_RUN_TOOLS or tool not in granted), None)) is not None:
        return report.model_copy(update={"no_cases": False, "blocked_by": blocked})
    output_type = _output_type(step)
    if output_type is None:
        return report

    run = _runner(step, step_id, output_type, registry)
    dataset = CaseDataset.from_dict(dict(cases), custom_evaluator_types=[OutcomeMatches], default_name=step.name)
    for position, case in enumerate(dataset.cases, start=1):
        case.name = case.name or f"Case {position}"
    _refuse_unnamed_judges(dataset, step_id)
    before = len(calls)
    outside = _outside_names()
    results: list[CaseResult] = []
    for case in dataset.cases:
        identity = StepIdentity(node_id=f"{step_id}#{case.name}", tier=step.tier if isinstance(step, Briefing) else None)
        alone = CaseDataset(name=dataset.name, cases=[case], evaluators=dataset.evaluators)
        evaluation = await alone.evaluate(_as_its_own_step(run, identity), progress=False)
        (outcome,) = (*evaluation.cases, *evaluation.failures)
        results.append(case_result(outcome, outside=outside))
    report = report.model_copy(update={"results": tuple(results), "no_cases": False, "stand_in_calls": len(calls) - before})
    if not any(result.errored for result in report.results):
        reports.put(report)
    return report


def _refuse_unnamed_judges(dataset: CaseDataset, step_id: str) -> None:
    """Refuse a dataset holding an `LLMJudge` that names no model, which would grade on pydantic-evals' own default."""
    judged = [("every case", dataset.evaluators), *((f"case {case.name!r}", case.evaluators) for case in dataset.cases)]
    if unnamed := [where for where, evaluators in judged if any(isinstance(each, LLMJudge) and each.model is None for each in evaluators)]:
        msg = f"the cases of {step_id} cannot run: the LLMJudge for {', '.join(unnamed)} names no model. Give it a `model`."
        raise CaseRunRefusedError(msg)


type _Run = Callable[[dict[str, JsonValue]], Awaitable[object]]
"""One run of the step on one case's inputs."""


def _runner(step: Briefing | Program, step_id: str, output_type: type, registry: StepRegistry) -> _Run:
    """How the registry runs `step` on one case's inputs, refused before any case runs when it cannot."""
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


def _as_its_own_step(run: _Run, identity: StepIdentity) -> _Run:
    """`run` performed under `identity`, so no case reads the identity of the step that started the case run."""

    async def performed(inputs: dict[str, JsonValue]) -> object:
        with performing_step(identity):
            return await run(inputs)

    return performed


def _outside_names() -> frozenset[str]:
    """The class names of every error whose cause lies outside the step, subclasses included, read when called."""
    found: set[str] = set()
    pending: list[type[BaseException]] = list(_OUTSIDE_THE_STEP)
    while pending:
        error = pending.pop()
        if error.__name__ not in found:
            found.add(error.__name__)
            pending.extend(error.__subclasses__())
    return frozenset(found)


def _cases_address(cases: Mapping[str, JsonValue] | None) -> str:
    """The SHA-256 of the dataset's canonical JSON, so the same cases written two ways share one address."""
    canonical = json.dumps(to_jsonable_python(dict(cases)) if cases is not None else None, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
