"""Import a folder of workflow and step files into the library: steps first, then the workflows that use them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml

from core.ai.workflows.contracts import (
    DocumentError,
    LibraryError,
    StepVersionChangedError,
    Workflow,
    WorkflowPresentation,
    fingerprint,
    load_spec_document,
    spec_identity,
    step_content_address,
    step_page_id,
    verify_presentation,
    verify_savable,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from harness.features.browsing.exceptions import (
    WorkflowCardChangedError,
    WorkflowNameRepeatedError,
    WorkflowNotLiveError,
    WorkflowVersionChangedError,
    WorkflowVersionSupersededError,
    WorkflowVersionUnreadableError,
)
from harness.features.browsing.step_locks import as_stored, read_step_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import InlineStep, LibraryCollection, SpecDocument, StepDocument, StepRegistry, StepVersionQueries

    from harness.features.browsing.internal_contracts import SavedVersionQueries

type CaseCheck = Callable[[str, Mapping[str, JsonValue]], None]
"""Raises when a step's cases, given with the step's library id, could not run."""

_CASES = TypeAdapter(dict[str, JsonValue])
"""Validates a step's case file as JSON data under named keys."""

_USES = TypeAdapter(list[str])
"""Validates a workflow file's `uses` frontmatter key: the reusable step ids it names, if any."""

_PRESENTATION = TypeAdapter(WorkflowPresentation | None)
"""Validates a workflow file's `presentation` frontmatter key: the author's own words, or none."""


type SkipReason = Literal["moved_past", "archived"]
"""Why the import wrote nothing for a workflow file: `moved_past_by` saved a newer version, or the library archived the file's version."""


@dataclass(frozen=True)
class SkippedWorkflowFile:
    """A workflow file the import wrote nothing for, and why."""

    file: Path
    version: int
    """The version the file declares."""
    reason: SkipReason
    newer_id: str | None
    """The library's newest live version, `wf.<name>.v<n>`, to bring the file up to, when the reason is `moved_past`."""
    newer_version: int | None
    """That version's number, when the reason is `moved_past`."""


    def __post_init__(self) -> None:
        """Refuse a newer version on a skip that was not `moved_past`, and a `moved_past` skip without one."""
        if (self.reason == "moved_past") != (self.newer_id is not None and self.newer_version is not None) or (
            self.reason != "moved_past" and (self.newer_id is not None or self.newer_version is not None)
        ):
            msg = f"a moved_past skip names the newer version's id and number, and an {self.reason} skip names neither"
            raise ValueError(msg)


@dataclass(frozen=True)
class FolderImport:
    """What one import of a folder did: the ids the library holds from it, and the workflow files it skipped."""

    written: tuple[str, ...]
    """The id of every step and workflow the library holds from the folder, written now or already held unchanged.

    The reusable steps come first and then the workflows, each in sorted file order. A skipped file has no id here."""
    skipped: tuple[SkippedWorkflowFile, ...]
    """Each workflow file the import wrote nothing for and why, in sorted file order."""


@dataclass(frozen=True)
class _ReadyWorkflow:
    """One workflow file with its `uses` steps copied in and checked, waiting to be written."""

    file: Path
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
    moved_past_by: str,
    environment: Mapping[str, str] | None = None,
) -> FolderImport:
    """Write every step under `path/steps` and every workflow under `path/workflows` the library does not already hold.

    Every file under `path` is read, parsed, and (for a workflow) checked with `verify_savable`
    before anything is written: a workflow's `uses` steps are taken from this same folder when it
    carries them, otherwise fetched from what the library already holds. One bad file anywhere under
    `path` therefore refuses the whole folder rather than leaving a partial library.

    A step file is refused when the library would read its step back different, because the library drops null values
    and empty lists and mappings, and when the library holds its version with other content. A step's cases are read
    from `path/evals/<step name with hyphens>.yaml` and saved with it. When `path/evals` exists, a step with no case
    file there is saved with none, which removes any cases its version held. When it does not, every version keeps
    the cases it holds.

    A workflow file whose version the library holds is compared with it before the first write. An archived version is
    skipped and stays archived. When the library's newest live version of the workflow is higher, the file is skipped if
    `moved_past_by` saved that version, and refuses the folder otherwise. A superseded version with no newer live
    version refuses the folder. Other content at the same version refuses the folder, and so does the same workflow
    under another title, description, body or presentation. The same workflow under the same words is written only
    when its stamp changed, and then only the stamp, so the page keeps its lineage, its live trial marker and its
    consumer record.

    Each workflow is stamped with the fingerprint of the environment it was checked against,
    exactly as a save to the library stamps one, so a later change to a step, an agent or a grant reads
    as drift here too.

    Args:
        path: The directory holding `steps/` and `workflows/`.
        library: Where the reusable steps and the workflows are written.
        registry: What this folder is checked against, and what the fingerprint is taken over.
        by: Who imported this, recorded on every page.
        check_cases: Raises when a step's cases, given with the step's library id, could not run.
        moved_past_by: The `saved_by` of the one writer whose newer versions a workflow file may lag behind, such
            as `agent:step-maintenance`.
        environment: The stamps this host contributes to the fingerprint (`environment_stamps()`).
            `None` stamps the registry alone.

    Returns:
        The ids the library holds from the folder and the workflow files it skipped.

    Raises:
        SaveRefusedError: A workflow that would not run on this registry.
        PresentationRefusedError: A workflow whose `presentation` names an input or an artifact
            the workflow does not declare.
        StepVersionChangedError: The library holds a step file's version with other content. A note names the
            step file. Nothing is written.
        WorkflowNameRepeatedError: Two workflow files hold the same workflow. Nothing is written.
        WorkflowVersionSupersededError: The library's newest live version of a workflow file's workflow is higher
            than the file's, and someone other than `moved_past_by` saved it, or its page records nobody. Nothing is
            written.
        WorkflowNotLiveError: The library superseded a workflow file's version and no newer version is live. Nothing
            is written.
        WorkflowVersionUnreadableError: The library's copy of a workflow file's version no longer reads as a workflow.
            Nothing is written.
        WorkflowVersionChangedError: The library holds a workflow file's version with other content, after the drop of
            null values and empty lists and mappings the library applies. Nothing is written.
        WorkflowCardChangedError: The library holds a workflow file's workflow at its version under another title,
            description, body or presentation. Nothing is written.
        DocumentError: A step or a workflow document does not parse, a step would read back different, or a case
            file does not parse or `check_cases` refuses it. The message names its file.
        pydantic.ValidationError: A step, a workflow's `uses` list, or its `presentation` does not parse.
        LookupError: A workflow's agent step names its agent and `registry` resolves no agents. Nothing is written.
        LibraryError: A workflow names a `uses` step the library does not hold, which writes nothing, or the library
            refuses a write, which keeps the writes before it.
    """
    files = sorted((path / "steps").glob("*.md"))
    steps = [read_step_file(file) for file in files]
    cases = [_step_cases(path, document, check_cases) for document in steps]
    await _refuse_changed_versions(files, steps, library)
    by_id: dict[str, InlineStep] = {step_page_id(doc.step.name, doc.version): doc.step for doc in steps}

    ready = await _read_workflows(path, by_id, library, registry)
    held = [await _held(ready_workflow, library, moved_past_by=moved_past_by) for ready_workflow in ready]
    stamps = [fingerprint(ready_workflow.workflow, registry, environment=dict(environment or {})) for ready_workflow in ready]

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
    for ready_workflow, kept, stamp in zip(ready, held, stamps, strict=True):
        if isinstance(kept, SkippedWorkflowFile):
            continue
        if kept is None:
            written.append(
                await library.put(
                    ready_workflow.workflow,
                    title=ready_workflow.title,
                    summary=ready_workflow.summary,
                    applicability=ready_workflow.applicability,
                    provenance={"saved_by": by},
                    fingerprint=stamp,
                    presentation=ready_workflow.presentation,
                )
            )
        else:
            await library.restamp(kept, stamp)
            written.append(kept)
    return FolderImport(written=tuple(written), skipped=tuple(kept for kept in held if isinstance(kept, SkippedWorkflowFile)))


async def _read_workflows(
    path: Path, by_id: Mapping[str, InlineStep], library: LibraryCollection, registry: StepRegistry
) -> list[_ReadyWorkflow]:
    """Every workflow file under `path/workflows`, its `uses` steps copied in and checked, refusing two files of one workflow."""
    ready: list[_ReadyWorkflow] = []
    read: dict[str, Path] = {}
    for file in sorted((path / "workflows").glob("*.md")):
        document = _spec_document(file)
        if document.workflow.name in read:
            raise WorkflowNameRepeatedError(read[document.workflow.name], file, document.workflow.name)
        read[document.workflow.name] = file
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
                file=file,
                workflow=workflow,
                title=str(document.frontmatter.get("title") or workflow.name),
                summary=str(document.frontmatter.get("description") or workflow.description),
                applicability=document.body.strip(),
                presentation=presentation,
            )
        )
    return ready


async def _held(ready: _ReadyWorkflow, library: SavedVersionQueries, *, moved_past_by: str) -> str | SkippedWorkflowFile | None:
    """The id of the unchanged version the library holds the file under, the skip, or None to write the file."""
    workflow = ready.workflow
    held_id = await library.locate(workflow.name, workflow.version)
    card = None if held_id is None else await library.fetch_card(held_id)
    if held_id is None or card is None:
        return None
    if card.archived_at is not None:
        return SkippedWorkflowFile(file=ready.file, version=workflow.version, reason="archived", newer_id=None, newer_version=None)
    if (moved_past := await _moved_past(ready, held_id, library, moved_past_by=moved_past_by)) is not None:
        return moved_past
    live = await library.fetch_card(workflow.name)
    if live is None or live.id != held_id:
        highest = max(version for version, _ in await library.versions(workflow.name))
        raise WorkflowNotLiveError(ready.file, held_id, highest_version=highest)
    try:
        stored = await library.fetch_workflow(held_id)
    except ValidationError as error:
        first = error.errors()[0]
        reason = f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
        raise WorkflowVersionUnreadableError(ready.file, held_id, reason) from error
    except LibraryError as error:
        raise WorkflowVersionUnreadableError(ready.file, held_id, " ".join(str(error).split())) from error
    if spec_identity(stored) not in {spec_identity(workflow), _stored_address(workflow)}:
        raise WorkflowVersionChangedError(ready.file, held_id)
    words = {
        "title": (ready.title, card.title),
        "description": (ready.summary, card.summary),
        "body": (ready.applicability, card.applicability.strip()),
        "presentation": (ready.presentation, card.presentation),
    }
    if differing := [name for name, (in_file, saved) in words.items() if in_file != saved]:
        raise WorkflowCardChangedError(ready.file, held_id, differing)
    return held_id


async def _moved_past(ready: _ReadyWorkflow, held_id: str, library: SavedVersionQueries, *, moved_past_by: str) -> SkippedWorkflowFile | None:
    """The skip when `moved_past_by` saved the newest live version above the file's, None when no live version is above it."""
    newest_id = await library.current(held_id)
    if newest_id == held_id:
        return None
    newest_version = {version_id: version for version, version_id in await library.versions(ready.workflow.name)}.get(newest_id, 0)
    if newest_version <= ready.workflow.version:
        return None
    try:
        saved_by = await library.saved_by(newest_id)
    except LibraryError as error:
        raise WorkflowVersionSupersededError(
            ready.file, held_id, newest_id=newest_id, newest_version=newest_version, saved_by="a writer its page does not record"
        ) from error
    if saved_by != moved_past_by:
        raise WorkflowVersionSupersededError(ready.file, held_id, newest_id=newest_id, newest_version=newest_version, saved_by=saved_by)
    return SkippedWorkflowFile(file=ready.file, version=ready.workflow.version, reason="moved_past", newer_id=newest_id, newer_version=newest_version)


def _stored_address(workflow: Workflow) -> str | None:
    """The content address of `workflow` as the library reads it back, or None when that no longer validates."""
    try:
        return spec_identity(Workflow.model_validate(as_stored(workflow.model_dump(mode="json"))))
    except ValidationError:
        return None


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
