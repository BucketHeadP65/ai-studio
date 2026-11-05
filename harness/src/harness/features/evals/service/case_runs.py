"""Run a reusable step's own cases once per content address, through the registry's own executor, and keep the report.

Run them through a registry built with stand-ins, so every write the step makes is checked and recorded and nothing
lands in a knowledge base.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING, get_type_hints

from core.ai.workflows.contracts import AgentStep, Briefing, Program, Shape, StepIdentity, build_type, performing_step, step_content_address
from core.connection_failures import connection_failure_message
from pydantic import ValidationError
from pydantic_ai.exceptions import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelContextWindowExceededError,
    ModelHTTPError,
    ModelNotFoundError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    ModelUnavailableError,
)
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

_OUTSIDE_THE_STEP: tuple[type[BaseException], ...] = (
    ModelConnectionError,
    ModelRequestTimeout,
    ModelAuthenticationError,
    ModelNotFoundError,
    ModelServerError,
    ModelUnavailableError,
    ModelRateLimitError,
    ModelQuotaExceededError,
    TimeoutError,
    SpendLimitExceeded,
)
"""The errors whose cause lies outside the step: a model connection or timeout error, a provider error for the
deployment's own setup (a refused key, a model not found, a server error), a rate limit, an exhausted quota or credit,
a timeout, and the deployment's spend limit."""

_SERVER_ERROR = 500
"""The lowest HTTP status of a provider's server error, which the provider's setup and not the step caused."""

_DATABASE = "a knowledge base database"
"""How a database connection failure inside a read tool is named, in a message nothing here shows."""


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
    the cases make is counted. A case errored when its run stopped for a cause outside the step: a model connection or
    timeout error, a provider error for the deployment's own setup (a refused key, a model not found, a 5xx), a rate
    limit, an exhausted quota or credit, a timeout, the deployment's spend limit, or a database connection failure. A new report is kept through `reports`,
    so the same step and cases never run twice, unless a case errored: that report is returned and not kept, so the
    cases run again next time. A kept report is returned under `step_id`. Nothing runs, and nothing is kept, for a
    step with no cases, an agent step, a step whose output is another step's type, or a step holding a tool outside
    `CASE_RUN_TOOLS` or outside `registry.grants`, which the report names in `blocked_by`.

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
        CaseRunRefusedError: `registry` cannot perform a briefing or run a program, the cases do not load as a
            dataset, two cases share a name, the dataset declares report evaluators, or an `LLMJudge` in it names no
            model. Nothing has run.
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
    dataset = _dataset(cases, step_id)
    before = len(calls)
    outside = _outside_names()
    results: list[CaseResult] = []
    for case in dataset.cases:
        identity = StepIdentity(node_id=f"{step_id}#{case.name}", tier=step.tier if isinstance(step, Briefing) else None)
        alone = CaseDataset(name=dataset.name, cases=[case], evaluators=dataset.evaluators)
        stopped: list[Exception] = []
        evaluation = await alone.evaluate(_as_its_own_step(run, identity, stopped), progress=False)
        (outcome,) = (*evaluation.cases, *evaluation.failures)
        results.append(case_result(outcome, run_errored=any(_is_outside_the_step(error) for error in stopped), outside=outside))
    report = report.model_copy(update={"results": tuple(results), "no_cases": False, "stand_in_calls": len(calls) - before})
    if not any(result.errored for result in report.results):
        reports.put(report)
    return report


def check_step_cases(step_id: str, cases: Mapping[str, JsonValue]) -> None:
    """Refuse a step's cases that `perform_step_cases` would refuse before running any of them.

    The cases must load as a `pydantic_evals` dataset with `OutcomeMatches` registered, with no two cases sharing a
    name, no report evaluators and no `LLMJudge` without a model. Nothing runs.

    Args:
        step_id: The step's id in the library, which a refusal names.
        cases: The step's dataset in `pydantic_evals` form.

    Raises:
        CaseRunRefusedError: The cases would not run. The message says what to change.
    """
    _dataset(cases, step_id)


def _dataset(cases: Mapping[str, JsonValue], step_id: str) -> CaseDataset:
    """The cases as a dataset every case of which can run, each named, refused as `CaseRunRefusedError` otherwise."""
    try:
        dataset = CaseDataset.from_dict(_named(cases, step_id), custom_evaluator_types=[OutcomeMatches], default_name=step_id)
    except ValidationError as error:
        problems = ", ".join(f"{'.'.join(str(part) for part in problem['loc']) or 'the dataset'}: {problem['msg']}" for problem in error.errors())
        msg = f"the cases of {step_id} do not load as a dataset ({problems}). Fix the case file."
        raise CaseRunRefusedError(msg) from error
    except ExceptionGroup as error:
        problems = ", ".join(str(problem) for problem in error.exceptions)
        msg = f"the cases of {step_id} name an evaluator that does not load ({problems}). Fix the case file."
        raise CaseRunRefusedError(msg) from error
    _refuse_unjudgeable(dataset, step_id)
    return dataset


def _named(cases: Mapping[str, JsonValue], step_id: str) -> dict[str, JsonValue]:
    """The dataset with every unnamed case named `Case <position>`, refused when two cases then share a name."""
    listed = cases.get("cases")
    if not isinstance(listed, list):
        return dict(cases)
    named: list[JsonValue] = [
        {**case, "name": f"Case {position}"} if isinstance(case, dict) and not case.get("name") else case
        for position, case in enumerate(listed, start=1)
    ]
    names = [str(case.get("name")) for case in named if isinstance(case, dict)]
    if repeated := sorted({name for name in names if names.count(name) > 1}):
        msg = (
            f"the cases of {step_id} cannot run: more than one case is named {', '.join(repr(name) for name in repeated)}, "
            f"counting an unnamed case as `Case <position>`. Give every case its own name."
        )
        raise CaseRunRefusedError(msg)
    return {**cases, "cases": named}


def _refuse_unjudgeable(dataset: CaseDataset, step_id: str) -> None:
    """Refuse a dataset declaring report evaluators, or holding an `LLMJudge` that names no model."""
    if dataset.report_evaluators:
        declared = ", ".join(type(evaluator).__name__ for evaluator in dataset.report_evaluators)
        msg = (
            f"the cases of {step_id} cannot run: the dataset declares report evaluators ({declared}). A step's cases are "
            f"judged one case at a time, so report evaluators do not run here. Remove them."
        )
        raise CaseRunRefusedError(msg)
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


def _as_its_own_step(run: _Run, identity: StepIdentity, stopped: list[Exception]) -> _Run:
    """`run` performed under `identity`, never the caller's, appending any error it raises to `stopped` before re-raising it."""

    async def performed(inputs: dict[str, JsonValue]) -> object:
        with performing_step(identity):
            try:
                return await run(inputs)
            except Exception as error:
                stopped.append(error)
                raise

    return performed


def _is_outside_the_step(error: Exception) -> bool:
    """Whether `error` stopped a case's run for a cause outside the step. A context window overflow is the step's own."""
    if isinstance(error, ModelContextWindowExceededError):
        return False
    if isinstance(error, _OUTSIDE_THE_STEP):
        return True
    if isinstance(error, ModelHTTPError) and error.status_code >= _SERVER_ERROR:
        return True
    return connection_failure_message(error, database=_DATABASE) is not None


def _outside_names() -> frozenset[str]:
    """The class names of the errors whose cause lies outside the step, subclasses included, for an evaluator's raise."""
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
