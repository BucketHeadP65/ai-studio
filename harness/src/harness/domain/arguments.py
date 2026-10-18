"""Admitting what a run is given: its values against the workflow, its selections against this deployment.

Two checks, in this order. First every value against its parameter's own JSON Schema, which is
`core.ai.workflows.contracts.normalize_arguments` and knows nothing about this deployment. Then every value a
first-party selector was declared over, against what this deployment actually holds. A value sent as
text reaches the selector check as the array its schema normalized it to.

A selector is declared on the saved workflow's presentation, never on the parameter. A launch with
no card behind it, such as a draft, has no selector to check, and the step's own read refuses a
name nothing answers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from core.ai.workflows.contracts import (
    KNOWLEDGE_BASE_CONTROL,
    ArgumentRefusedError,
    LibraryError,
    ParameterSchemaError,
    WorkflowArguments,
    normalize_arguments,
)
from pydantic import ValidationError

from harness.domain.exceptions import CardUnreadableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import SavedWorkflow, Workflow, WorkflowLibrary, WorkflowPresentation
    from pydantic import JsonValue


class KnowledgeBaseNames(Protocol):
    """Every knowledge base this deployment is registered to read, by the name a caller may select.

    Read fresh on every call: a knowledge base registered while this process was running is one a
    run may name on the very next launch, not only after a restart.
    """

    async def __call__(self) -> frozenset[str]:
        """The registered names.

        Returns:
            Every name a caller may select, read fresh.

        Raises:
            RuntimeError: the registry could not be read. An implementation never answers that
                with an empty set.
        """
        ...


AdmissionRefused = (ArgumentRefusedError, ParameterSchemaError, CardUnreadableError)
"""Every way admitting a run's arguments refuses, as the one `except` clause a caller writes."""


def _selected_names(value: JsonValue) -> tuple[tuple[str, str], ...]:
    """Each name a selecting parameter's value offers, with the JSON path it sits at, and none for any other shape."""
    if isinstance(value, str):
        return ((value, "$"),)
    if isinstance(value, list):
        return tuple((item, f"$[{index}]") for index, item in enumerate(value) if isinstance(item, str))
    return ()


def _selecting_parameters(presentation: WorkflowPresentation | None, supplied: Mapping[str, JsonValue]) -> list[str]:
    """Which supplied parameters the card declared a knowledge-base selector over, in the card's order."""
    if presentation is None:
        return []
    return [shown.key for shown in presentation.inputs if shown.control == KNOWLEDGE_BASE_CONTROL and shown.key in supplied]


async def _refuse_unregistered_selection(
    selecting: list[str], arguments: WorkflowArguments, names: KnowledgeBaseNames | None
) -> None:
    """Refuse the first selected knowledge base the registry does not hold, reading it once and only when something selects."""
    if not selecting:
        return
    if names is None:
        detail = "this deployment cannot read which knowledge bases are registered, so it will not start a run that names one"
        raise ArgumentRefusedError(selecting[0], "$", detail)
    try:
        registered = await names()
    except RuntimeError as exc:
        detail = f"the registry of knowledge bases could not be read, so no run may name one: {exc}"
        raise ArgumentRefusedError(selecting[0], "$", detail) from exc
    for parameter in selecting:
        for offered, path in _selected_names(arguments[parameter]):
            if offered not in registered:
                raise ArgumentRefusedError(parameter, path, f"{offered!r} is not a knowledge base registered here")


async def admitted_arguments(
    workflow: Workflow,
    supplied: Mapping[str, JsonValue],
    *,
    presentation: WorkflowPresentation | None,
    names: KnowledgeBaseNames | None = None,
) -> WorkflowArguments:
    """What `workflow` will be run with, admitted the way every entry that takes a caller's values admits them.

    Args:
        workflow: The workflow about to run.
        supplied: What the caller gave, by parameter name.
        presentation: The saved workflow's own words, which is where a first-party selector is
            declared. `None` for a launch with no card behind it, which declares none.
        names: Which knowledge bases are registered here, for a card that declares a selector.
            `None` refuses such a card rather than admitting an unchecked name.

    Returns:
        The arguments to launch with.

    Raises:
        ArgumentRefusedError: one value its parameter will not admit, or one selection this
            deployment is not registered for, naming the parameter and where inside the value the
            refusal is. It is also raised when the registry of knowledge bases cannot be read
            while a card declares a selector.
        ParameterSchemaError: one parameter's own declared schema is not usable.
    """
    selecting = _selecting_parameters(presentation, supplied)
    admitted = normalize_arguments(workflow, supplied)
    await _refuse_unregistered_selection(selecting, admitted, names)
    return admitted


def selected_knowledge_bases(presentation: WorkflowPresentation | None, arguments: Mapping[str, JsonValue]) -> frozenset[str]:
    """Every knowledge base `arguments` select through a parameter the card declares a knowledge-base selector over.

    A card with no selector, or no card at all, selects nothing.

    Args:
        presentation: The saved workflow's own words, where a selector is declared. `None` declares none.
        arguments: The values a schedule holds or a run was given, by parameter name.

    Returns:
        The selected names. A value that is neither text nor a list of text selects nothing.
    """
    return frozenset(name for parameter in _selecting_parameters(presentation, arguments) for name, _ in _selected_names(arguments[parameter]))


async def presentation_of(library: WorkflowLibrary, workflow_id: str) -> WorkflowPresentation | None:
    """The author's own words for one saved workflow, or `None` when this library keeps none.

    `None` covers exactly two answers: a library without the optional `fetch_card` verb, and a
    library with no card filed under this id. Both mean nobody declared anything. A read that
    fails is neither, and it raises.

    Args:
        library: The library the workflow is served from.
        workflow_id: The saved workflow's id.

    Returns:
        The card's presentation, or `None` when the library keeps no card for this id.

    Raises:
        CardUnreadableError: the library could not answer for this id.
    """
    fetch = getattr(library, "fetch_card", None)
    if fetch is None:
        return None
    try:
        card: SavedWorkflow | None = await fetch(workflow_id)
    except (LibraryError, KeyError, ValidationError) as exc:
        msg = f"the library could not say what {workflow_id!r} asks for, so nothing was started: {exc}"
        raise CardUnreadableError(msg) from exc
    return None if card is None else card.presentation
