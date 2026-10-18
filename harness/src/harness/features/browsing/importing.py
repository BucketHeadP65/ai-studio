"""Import a folder of workflow and step files into the library: steps first, then the workflows that use them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    DocumentError,
    WorkflowPresentation,
    fingerprint,
    load_spec_document,
    load_step_document,
    step_page_id,
    verify_presentation,
    verify_savable,
)
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import InlineStep, LibraryCollection, SpecDocument, StepDocument, StepRegistry, Workflow

_USES = TypeAdapter(list[str])
"""Validates a workflow file's `uses` frontmatter key: the reusable step ids it names, if any."""

_PRESENTATION = TypeAdapter(WorkflowPresentation | None)
"""Validates a workflow file's `presentation` frontmatter key: the author's own words, or none."""


@dataclass(frozen=True)
class _ReadyWorkflow:
    """One workflow file with its `uses` steps copied in and checked, waiting to be written."""

    workflow: Workflow
    title: str
    summary: str
    applicability: str
    uses: tuple[str, ...]
    presentation: WorkflowPresentation | None


async def import_path(
    path: Path, *, library: LibraryCollection, registry: StepRegistry, by: str, environment: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Write every step under `path/steps` and every workflow under `path/workflows`, returning the ids in order.

    Every file under `path` is read, parsed, and (for a workflow) checked with `verify_savable`
    before anything is written: a workflow's `uses` steps are taken from this same folder when it
    carries them, otherwise fetched from what the library already holds. One bad file anywhere under
    `path` therefore refuses the whole folder rather than leaving a partial library.

    Each workflow is stamped with the fingerprint of the environment it was checked against, exactly
    as a save to the library stamps one, so a later change to a step, an agent or a grant reads as
    drift here too.

    Args:
        path: The directory holding `steps/` and `workflows/`.
        library: Where the reusable steps and the workflows are written.
        registry: What this folder is checked against, and what the fingerprint is taken over.
        by: Who imported this, recorded on every page.
        environment: The stamps this host contributes to the fingerprint (`environment_stamps()`).
            `None` stamps the registry alone.

    Returns:
        The written ids, the reusable steps first and then the workflows, each in sorted file order.

    Raises:
        SaveRefusedError: A workflow that would not run on this registry.
        PresentationRefusedError: A workflow whose `presentation` names an input or an artifact
            the workflow does not declare.
        DocumentError: A step or a workflow document does not parse. The message names its file.
        pydantic.ValidationError: A step, a workflow's `uses` list, or its `presentation` does not parse.
    """
    steps = [_step_document(file) for file in sorted((path / "steps").glob("*.md"))]
    by_id: dict[str, InlineStep] = {step_page_id(doc.step.name, doc.version): doc.step for doc in steps}

    ready: list[_ReadyWorkflow] = []
    for file in sorted((path / "workflows").glob("*.md")):
        document = _spec_document(file)
        uses = tuple(_USES.validate_python(document.frontmatter.get("uses", [])))
        copied = tuple([by_id[step_id] if step_id in by_id else await library.fetch_step(step_id) for step_id in uses])
        workflow = document.workflow.model_copy(update={"inline": (*document.workflow.inline, *copied)})
        verify_savable(workflow, registry)  # the same check a save to the library runs before it writes
        # The file's own words, read as declared: a workflow that ships without them ships without
        # them, and the procedural `description` is never turned into a purpose it did not state.
        presentation = _PRESENTATION.validate_python(document.frontmatter.get("presentation"))
        if presentation is not None:
            verify_presentation(workflow, presentation)
        ready.append(
            _ReadyWorkflow(
                workflow=workflow,
                title=str(document.frontmatter.get("title") or workflow.name),
                summary=str(document.frontmatter.get("description") or workflow.description),
                applicability=document.body.strip(),
                uses=uses,
                presentation=presentation,
            )
        )

    written = [
        await library.put_step(
            doc.step,
            version=doc.version,
            title=doc.title,
            summary=doc.summary,
            applicability=doc.applicability,
            provenance={"saved_by": by},
        )
        for doc in steps
    ]
    written.extend(
        [
            await library.put(
                ready_workflow.workflow,
                title=ready_workflow.title,
                summary=ready_workflow.summary,
                applicability=ready_workflow.applicability,
                provenance={"saved_by": by, "uses": ",".join(ready_workflow.uses)},
                fingerprint=fingerprint(ready_workflow.workflow, registry, environment=dict(environment or {})),
                presentation=ready_workflow.presentation,
            )
            for ready_workflow in ready
        ]
    )
    return tuple(written)


def _step_document(file: Path) -> StepDocument:
    """Parse one step file, naming the file in any refusal."""
    try:
        return load_step_document(file.read_text(encoding="utf-8"))
    except DocumentError as error:
        msg = f"{file}: {error}"
        raise DocumentError(msg) from error


def _spec_document(file: Path) -> SpecDocument:
    """Parse one workflow file, naming the file in any refusal."""
    try:
        return load_spec_document(file.read_text(encoding="utf-8"))
    except DocumentError as error:
        msg = f"{file}: {error}"
        raise DocumentError(msg) from error
