"""Saving a workflow and reading its saved versions.

A save checks the workflow against this run's steps, then keeps it in a library stamped with this run's environment.
`version_hashes` reads the content address of every saved version of a workflow's name, which trust across versions
folds by.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.ai.workflows.domain.fingerprint import fingerprint, spec_identity
from core.ai.workflows.domain.validator import unsatisfied_parameters, validate
from core.ai.workflows.exceptions import LibraryError, SaveRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from core.ai.workflows.domain.intent import WorkflowIntent
    from core.ai.workflows.domain.lineage import Lineage
    from core.ai.workflows.domain.presentation import WorkflowPresentation
    from core.ai.workflows.domain.vocabulary import Workflow
    from core.ai.workflows.internal_contracts import WorkflowLibrary, WorkflowVersionQueries
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
            the host promotes. Step contracts and the tools the workflow's steps ask for are stamped regardless.
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


async def next_version(workflow: Workflow, *, versions: WorkflowVersionQueries | None) -> int:
    """The version number a new version of `workflow`'s name saves under: one above it and every saved version of the name.

    Archived and superseded versions count, so a new version never takes a number a saved version holds, and an
    adaptation of a version a revert made live again never lands on the archived version above it.

    Args:
        workflow: The version a new one is made from.
        versions: The library's read of every saved version of a name. None numbers the new version one above `workflow`.

    Returns:
        The new version's number.

    Raises:
        LibraryError: The library cannot read the saved versions.
    """
    saved = await versions.versions(workflow.name) if versions is not None else ()
    return max((workflow.version, *(version for version, _ in saved))) + 1


async def version_hashes(
    workflow: Workflow, *, versions: WorkflowVersionQueries | None, fetch_workflow: Callable[[str], Awaitable[Workflow]]
) -> dict[str, int]:
    """The content address of every saved version of `workflow`'s name that reads, mapped to its version number.

    `workflow` stands for its own version under its own content address and is never read again. A version
    `fetch_workflow` refuses with `LibraryError` or `ValidationError` is left out, and so is one whose workflow names a
    version other than the one its id is listed under. The other versions are read at once.

    Args:
        workflow: The version asked about.
        versions: The library's read of every saved version of a name. None counts `workflow` alone.
        fetch_workflow: Reads one saved version by its id.

    Returns:
        Content address to version number, holding `workflow`'s own at least.
    """
    hashes = {spec_identity(workflow): workflow.version}
    if versions is None:
        return hashes

    async def address(version: int, workflow_id: str) -> str | None:
        try:
            saved = await fetch_workflow(workflow_id)
        except (LibraryError, ValidationError):
            return None
        return spec_identity(saved) if saved.version == version else None

    others = [(version, workflow_id) for version, workflow_id in await versions.versions(workflow.name) if version != workflow.version]
    addresses = await asyncio.gather(*(address(version, workflow_id) for version, workflow_id in others))
    hashes.update({spec_hash: version for (version, _), spec_hash in zip(others, addresses, strict=True) if spec_hash is not None})
    return hashes
