"""Import a folder of workflow and step files into the library: steps first, then the workflows that use them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from core.ai.workflows.contracts import (
    DocumentError,
    StepVersionChangedError,
    WorkflowPresentation,
    fingerprint,
    load_spec_document,
    step_content_address,
    step_page_id,
    verify_presentation,
    verify_savable,
)
from pydantic import JsonValue, TypeAdapter

from harness.features.browsing.step_locks import read_step_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import (
        InlineStep,
        LibraryCollection,
        SpecDocument,
        StepDocument,
        StepRegistry,
        StepVersionQueries,
        Workflow,
    )

type CaseCheck = Callable[[str, Mapping[str, JsonValue]], None]
"""Raises when a step's cases, given with the step's library id, could not run."""

_CASES = TypeAdapter(dict[str, JsonValue])
"""Validates a step's case file as JSON data under named keys."""

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
    presentation: WorkflowPresentation | None


async def import_path(  # noqa: PLR0913 (each argument is a separate term the caller sets)
    path: Path,
    *,
    library: LibraryCollection,
    registry: StepRegistry,
    by: str,
    check_cases: CaseCheck,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Write every step under `path/steps` and every workflow under `path/workflows`, returning the ids in order.

    Every file under `path` is read, parsed, and (for a workflow) checked with `verify_savable`
    before anything is written: a workflow's `uses` steps are taken from this same folder when it
    carries them, otherwise fetched from what the library already holds. One bad file anywhere under
    `path` therefore refuses the whole folder rather than leaving a partial library.

    A step file is refused when the library would read its step back different, because the library drops null values
    and empty lists and mappings, and when the library holds its version with other content. A step's cases are read
    from `path/evals/<step name with hyphens>.yaml` and saved with it. When `path/evals` exists, a step with no case
    file there is saved with none, which removes any cases its version held. When it does not, every version keeps
    the cases it holds.

    Each workflow is stamped with the fingerprint of the environment it was checked against, exactly
    as a save to the library stamps one, so a later change to a step, an agent or a grant reads as
    drift here too.

    Args:
        path: The directory holding `steps/` and `workflows/`.
        library: Where the reusable steps and the workflows are written.
        registry: What this folder is checked against, and what the fingerprint is taken over.
        by: Who imported this, recorded on every page.
        check_cases: Raises when a step's cases, given with the step's library id, could not run.
        environment: The stamps this host contributes to the fingerprint (`environment_stamps()`).
            `None` stamps the registry alone.

    Returns:
        The written ids, the reusable steps first and then the workflows, each in sorted file order.

    Raises:
        SaveRefusedError: A workflow that would not run on this registry.
        PresentationRefusedError: A workflow whose `presentation` names an input or an artifact
            the workflow does not declare.
        StepVersionChangedError: The library holds a step file's version with other content. A note names the
            step file. Nothing is written.
        DocumentError: A step or a workflow document does not parse, a step would read back different, or a case
            file does not parse or `check_cases` refuses it. The message names its file.
        pydantic.ValidationError: A step, a workflow's `uses` list, or its `presentation` does not parse.
    """
    files = sorted((path / "steps").glob("*.md"))
    steps = [read_step_file(file) for file in files]
    cases = [_step_cases(path, document, check_cases) for document in steps]
    await _refuse_changed_versions(files, steps, library)
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
            cases=step_cases,
        )
        for doc, step_cases in zip(steps, cases, strict=True)
    ]
    written.extend(
        [
            await library.put(
                ready_workflow.workflow,
                title=ready_workflow.title,
                summary=ready_workflow.summary,
                applicability=ready_workflow.applicability,
                provenance={"saved_by": by},
                fingerprint=fingerprint(ready_workflow.workflow, registry, environment=dict(environment or {})),
                presentation=ready_workflow.presentation,
            )
            for ready_workflow in ready
        ]
    )
    return tuple(written)


def _step_cases(path: Path, document: StepDocument, check_cases: CaseCheck) -> Mapping[str, JsonValue] | None:
    """The step's case file read and checked, an empty mapping when `path/evals` holds none, or None with no `path/evals`."""
    if not (path / "evals").is_dir():
        return None
    file = path / "evals" / f"{document.step.name.replace('_', '-')}.yaml"
    if not file.is_file():
        return {}
    try:
        cases = _CASES.validate_python(yaml.safe_load(file.read_text(encoding="utf-8")))
        check_cases(step_page_id(document.step.name, document.version), cases)
    except Exception as error:
        msg = f"{file}: {error}"
        raise DocumentError(msg) from error
    return cases


async def _refuse_changed_versions(files: Sequence[Path], steps: Sequence[StepDocument], library: StepVersionQueries) -> None:
    """Refuse the first step whose version the library already holds with another content address, noting its file."""
    saved = await library.step_versions_of([document.step.name for document in steps])
    for file, document in zip(files, steps, strict=True):
        held = next((version for version in saved[document.step.name] if version.version == document.version), None)
        if held is not None and held.step_hash != step_content_address(document.step):
            error = StepVersionChangedError(held.step_id)
            error.add_note(f"The step file is {file}.")
            raise error


def _spec_document(file: Path) -> SpecDocument:
    """Parse one workflow file, naming the file in any refusal."""
    try:
        return load_spec_document(file.read_text(encoding="utf-8"))
    except DocumentError as error:
        msg = f"{file}: {error}"
        raise DocumentError(msg) from error
