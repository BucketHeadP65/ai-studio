"""Perform a step's declared examples through the same executor a workflow run uses.

A step's dataset is one YAML file named after the step, in `pydantic_evals.Dataset.from_file` form.
Loading the dataset, resolving the step in the library and running it are separate calls.
A caller can load and resolve without paying for the model calls a run makes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Briefing, Shape, build_type

from harness.features.evals.exceptions import StepNotABriefingError, StepOutputReusesAnotherError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import PerformStep
    from pydantic_evals import Dataset
    from pydantic_evals.reporting import EvaluationReport

    from harness.features.evals.internal_contracts import ReusableStepQueries


async def resolve_step(library: ReusableStepQueries, step_id: str) -> Briefing:
    """The briefing `step_id` names, read from the library the same way `save` reads one back.

    Args:
        library: The library to read the step from.
        step_id: The reusable step's id.

    Returns:
        The briefing.

    Raises:
        LibraryError: no reusable step has that id.
        StepNotABriefingError: The library holds `step_id` as a program or an agent step. This
            runner performs briefings only.
    """
    step = await library.fetch_step(step_id)
    if not isinstance(step, Briefing):
        msg = f"{step_id!r} is not a briefing (it is a {type(step).__name__}). harness evals run performs briefings only."
        raise StepNotABriefingError(msg)
    return step


def _output_type(step: Briefing) -> type:
    """The step's own result type, built from its declared shape."""
    if not isinstance(step.output, Shape):
        msg = f"{step.name!r} reuses another step's output type. harness evals run needs a step whose output is its own shape."
        raise StepOutputReusesAnotherError(msg)
    return build_type(step.output.json_schema, name=step.name, where=f"inline[{step.name}].output")


async def run_step_evals(dataset: Dataset, perform: PerformStep, step: Briefing) -> EvaluationReport:
    """Perform every case in `dataset` by calling `perform` the way a workflow run calls it.

    Each case's `inputs` run against the step's own output type, through the same worker a graph
    node would call for this step.

    Args:
        dataset: The step's cases.
        perform: The executor a workflow run performs a step through.
        step: The briefing to perform.

    Returns:
        The evaluation report over every case.

    Raises:
        StepOutputReusesAnotherError: The step's output names another step's type (a `like` slot)
            instead of declaring its own shape.
    """
    output_type = _output_type(step)

    async def task(inputs: Mapping[str, object]) -> object:
        return await perform(step, output_type, **inputs)

    return await dataset.evaluate(task)
