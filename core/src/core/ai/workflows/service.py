"""Saving a workflow: check it against this run's steps, then keep it in a library stamped with this run's environment."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.domain.fingerprint import fingerprint
from core.ai.workflows.domain.validator import unsatisfied_parameters, validate
from core.ai.workflows.exceptions import SaveRefusedError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.domain.intent import WorkflowIntent
    from core.ai.workflows.domain.lineage import Lineage
    from core.ai.workflows.domain.presentation import WorkflowPresentation
    from core.ai.workflows.domain.vocabulary import Workflow
    from core.ai.workflows.internal_contracts import WorkflowLibrary
    from core.ai.workflows.registry import StepRegistry


def verify_savable(workflow: Workflow, registry: StepRegistry, *, supplied_parameters: Sequence[str] = ()) -> None:
    """Refuse a workflow that does not validate against this run's steps or needs parameters nobody supplies.

    Every write goes through this check, whatever transport carries it. A workflow's inline steps
    are refused here too when they do not build.

    Args:
        workflow: The workflow to check.
        registry: This run's steps.
        supplied_parameters: Parameter names a caller will provide. Empty skips the parameter check.

    Raises:
        SaveRefusedError: If the workflow does not validate, or requires parameters nobody supplies.
    """
    report = validate(workflow, registry)
    missing = unsatisfied_parameters(workflow, supplied_parameters) if supplied_parameters else ()
    if not report.ok or missing:
        raise SaveRefusedError(report, missing)


async def save(
    workflow: Workflow,
    library: WorkflowLibrary,
    registry: StepRegistry,
    *,
    title: str,
    summary: str,
    applicability: str,
    saved_by: str,
    supplied_parameters: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    lineage: Lineage | None = None,
    presentation: WorkflowPresentation | None = None,
    intent: WorkflowIntent | None = None,
) -> str:
    """Validate a workflow against this run's environment, then keep it stamped with that environment.

    Args:
        workflow: The workflow to keep.
        library: Where to keep it.
        registry: This run's steps. A workflow naming a step that does not exist is refused.
        title: Human-facing name.
        summary: One sentence, for the card a chooser scans.
        applicability: When to use this and when not to.
        saved_by: Who is saving (`human:<id>`, or a tool identifier). Recorded, not inferred.
        supplied_parameters: Parameter names a caller will provide, if you want that checked too.
        environment: Extra stamps for the fingerprint, such as capability versions and whatever else
            the host promotes. Step contracts and grants are stamped regardless.
        lineage: The typed diff that produced this workflow, if it is an adaptation.
        presentation: The author's words about this workflow, if it travels with any.
        intent: What the person who wanted this automation asked for, if it travels with one. It
            is recorded beside the workflow and grants nothing.

    Returns:
        The library's id for the workflow.

    Raises:
        SaveRefusedError: If the workflow does not validate, or requires parameters nobody supplies.
        PresentationRefusedError: If the presentation names an input or an artifact the workflow
            does not declare.
    """
    verify_savable(workflow, registry, supplied_parameters=supplied_parameters)
    return await library.put(
        workflow,
        title=title,
        summary=summary,
        applicability=applicability,
        provenance={"saved_by": saved_by},
        fingerprint=fingerprint(workflow, registry, environment=environment),
        lineage=lineage,
        presentation=presentation,
        intent=intent,
    )
