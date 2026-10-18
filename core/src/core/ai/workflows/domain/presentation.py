"""A workflow's presentation on its library card, and the check it passes before a library keeps it.

A presentation is the author's own words about the workflow, for the surfaces a person meets it on.
It is closed data (prose fields, input labels, display-only result paths, and actions drawn from a
fixed set), and every word in it comes from whoever wrote the workflow. Nothing here supplies one.

A card may carry an **intent** beside it: what the person who wanted this automation asked for,
and what they expect a run to do. The two are not the same thing. A presentation is what a reader
is shown, an intent is what was asked for, and neither enters the workflow's identity or the
grants it runs under. Both are kept only if they were written, and absent stays absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.ai.workflows.exceptions import PresentationRefusedError

if TYPE_CHECKING:

    from pydantic.json_schema import JsonSchemaValue

    from core.ai.workflows.domain.vocabulary import Workflow


ActionId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""An artifact action's own name, stripped on the way in.

The id a duplicate check compares and the id a press is recorded under are the same stripped string.
A name that is blank after stripping is refused."""


InputControl = Literal["text", "textarea", "knowledge-base", "json"]
"""Which control a person answers one parameter through. The set is closed, and a host implements
every member of it.

`knowledge-base` is the one that means more than a shape: it says the values are names of knowledge
bases the host is registered to read, and a host that holds such a registry checks them against it
before a run starts. The other three are shapes alone."""


class InputPresentation(BaseModel):
    """One of the workflow's parameters, as a person is asked for it.

    `key` names a parameter the workflow declares, and a key naming nothing is refused before a write.
    A change to how a person is asked never changes the workflow's identity or its evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    control: InputControl | None = None
    """Which control to answer through. `None` leaves the choice to whatever reads the schema."""


class ResultFieldPresentation(BaseModel):
    """One recorded-output fact a reader is shown after a run, under the author's own label.

    `path` addresses the recorded outcome for display and nothing else: it selects no work, grants
    no access, and is never resolved against the workflow.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SaveNoteAction(BaseModel):
    """Open one declared artifact for the person to keep, at a destination they pick themselves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["save_note"] = "save_note"
    id: ActionId
    label: str
    artifact_id: str
    destination: Literal["user_selects"] = "user_selects"


class RecordOwnerReviewAction(BaseModel):
    """Record the owner's own reading of one or more declared artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["record_owner_review"] = "record_owner_review"
    id: ActionId
    label: str
    artifact_ids: tuple[str, ...]


ArtifactActionSlot = Annotated[SaveNoteAction | RecordOwnerReviewAction, Field(discriminator="kind")]
"""The closed set of intents an artifact action may carry.

Each member names a first-party handler the host already implements, and there is nowhere to put an
endpoint, a method, or a request body. A presentation therefore cannot make the host call anything
its own code does not already do."""


class WorkflowPresentation(BaseModel):
    """The author's words about one workflow, for the surfaces a person meets it on.

    Every field is the author's, and an absent field is absent: a reader that needs prose here
    shows nothing rather than a sentence this code composed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purpose: str = ""
    """What the workflow is for, in the words of whoever wrote it."""
    produces: str = ""
    """What a completed run leaves the person holding."""
    owner_participation: str = ""
    """What the person themselves does during a run."""
    not_for: str = ""
    """What to reach for something else for."""
    inputs: tuple[InputPresentation, ...] = ()
    result_fields: tuple[ResultFieldPresentation, ...] = ()
    actions: tuple[ArtifactActionSlot, ...] = ()

    @model_validator(mode="after")
    def _every_name_is_its_own(self) -> WorkflowPresentation:
        """One label per parameter, one id per action, and a review that reviews something."""
        labelled: set[str] = set()
        for shown in self.inputs:
            if shown.key in labelled:
                msg = f"two inputs label the parameter {shown.key!r}; a parameter is asked for once"
                raise ValueError(msg)
            labelled.add(shown.key)
        named: set[str] = set()
        for action in self.actions:
            if action.id in named:
                msg = f"two artifact actions share the id {action.id!r}; an action's id is how a press is recorded"
                raise ValueError(msg)
            named.add(action.id)
            if isinstance(action, RecordOwnerReviewAction) and not action.artifact_ids:
                msg = f"action {action.id!r} records a review of nothing. Name the artifacts it reviews"
                raise ValueError(msg)
        return self


KNOWLEDGE_BASE_CONTROL: Literal["knowledge-base"] = "knowledge-base"
"""The one `InputControl` that says what the values mean rather than what shape they are.

It says the values are names of knowledge bases the host is registered to read. Which names those
are is the host's question and is checked at its own boundary. All this library asks is that the
parameter could hold one."""


def _admits_a_name(schema: JsonSchemaValue) -> bool:
    """Whether `schema` could hold the name of a knowledge base, or a list of them, passing what it does not say."""
    declared = schema.get("type")
    kinds = {declared} if isinstance(declared, str) else set(declared) if isinstance(declared, list) else set()
    if "string" in kinds:
        return True
    if "array" in kinds:
        items = schema.get("items")
        return not isinstance(items, dict) or _admits_a_name(items)
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and any(isinstance(branch, dict) and _admits_a_name(branch) for branch in branches):
            return True
    if "const" in schema:
        return isinstance(schema["const"], str)
    choices = schema.get("enum")
    if isinstance(choices, list):
        return all(isinstance(choice, str) for choice in choices)
    return not kinds


def verify_presentation(workflow: Workflow, presentation: WorkflowPresentation) -> None:
    """The check a presentation passes before any library keeps it.

    Checks the three things only the workflow can answer: that each labelled input names a parameter the
    workflow declares, that a parameter answered through the knowledge-base selector could hold a
    name, and that every action names an artifact the workflow's result declares.

    Args:
        workflow: The workflow the presentation describes.
        presentation: The presentation to check.

    Raises:
        PresentationRefusedError: If any input, selector or artifact id names nothing the workflow
            can answer for, listing every one.
    """
    declared_artifacts = {artifact.id for artifact in (workflow.result.artifacts if workflow.result is not None else ())}
    problems = [
        f"input {shown.key!r} names no parameter of {workflow.name!r}; it declares {sorted(workflow.parameters) or 'none'}"
        for shown in presentation.inputs
        if shown.key not in workflow.parameters
    ]
    problems.extend(
        f"input {shown.key!r} is answered through the knowledge-base selector, but {workflow.name!r} declares it with a schema "
        f"that cannot hold a name"
        for shown in presentation.inputs
        if shown.control == KNOWLEDGE_BASE_CONTROL
        and shown.key in workflow.parameters
        and not _admits_a_name(workflow.parameters[shown.key].json_schema)
    )
    for action in presentation.actions:
        wanted = (action.artifact_id,) if isinstance(action, SaveNoteAction) else action.artifact_ids
        problems.extend(
            f"action {action.id!r} names artifact {artifact_id!r}, which {workflow.name!r} does not declare. "
            f"it declares {sorted(declared_artifacts) or 'none'}"
            for artifact_id in wanted
            if artifact_id not in declared_artifacts
        )
    if problems:
        msg = "refused to keep a presentation that names what the workflow does not declare:\n" + "\n".join(f"  {line}" for line in problems)
        raise PresentationRefusedError(msg)


