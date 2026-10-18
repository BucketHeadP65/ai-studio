"""The browsing use cases: archive a workflow, restore it, and rewrite its card. None of them changes the workflow a run executes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import LibraryError, SavedWorkflow, prepare
from core.observability import get_logger
from pydantic import ValidationError

from harness.domain.contracts import card_readiness, effective_presentation, refusal_text
from harness.features.browsing.exceptions import CardRefusedError

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Workflow

    from harness.domain.contracts import DraftReadiness, ReadinessField
    from harness.features.browsing.api_models import WorkflowCardPatch
    from harness.session import Runtime

_logger = get_logger(__name__)


@dataclass(frozen=True)
class RedescribedCard:
    """A card after a rewrite, beside the readiness reading it was written on."""

    card: SavedWorkflow
    readiness: DraftReadiness


async def saved_card(session: Runtime, workflow_id: str, workflow: Workflow) -> SavedWorkflow:
    """The card the library holds for a workflow, or one made from its name and description.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.
        workflow: The workflow the card describes.

    Returns:
        The held card, or one made from the workflow when the library holds none or cannot be asked.
    """
    fetch = getattr(session.library, "fetch_card", None)
    if fetch is None:
        return SavedWorkflow(id=workflow_id, title=workflow.name, summary=workflow.description)
    try:
        found = await fetch(workflow_id)
    except (LibraryError, KeyError, ValidationError):
        return SavedWorkflow(id=workflow_id, title=workflow.name, summary=workflow.description)
    return found if found is not None else SavedWorkflow(id=workflow_id, title=workflow.name, summary=workflow.description)


async def archive_workflow(session: Runtime, workflow_id: str) -> SavedWorkflow:
    """Take one workflow out of the library. It leaves search and the listing, and its runs and versions stay.

    The agent is offered this only behind the person's approval.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.

    Returns:
        The refreshed card.

    Raises:
        LibraryError: If the library does not hold the workflow.
    """
    await session.library.set_archived(workflow_id, archived=True)
    _logger.info("workflow {workflow_id} archived", workflow_id=workflow_id)
    return await saved_card(session, workflow_id, await session.library.fetch_workflow(workflow_id))


async def restore_workflow(session: Runtime, workflow_id: str) -> SavedWorkflow:
    """Put one archived workflow back in the library, where search and the listing see it again.

    The agent is offered this only behind the person's approval.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.

    Returns:
        The refreshed card.

    Raises:
        LibraryError: If the library does not hold the workflow.
    """
    await session.library.set_archived(workflow_id, archived=False)
    _logger.info("workflow {workflow_id} restored", workflow_id=workflow_id)
    return await saved_card(session, workflow_id, await session.library.fetch_workflow(workflow_id))


async def redescribe_workflow(session: Runtime, workflow_id: str, body: WorkflowCardPatch) -> RedescribedCard:
    """Rewrite one workflow's card: its title, summary, applicability and the author's own words.

    The patch is merged onto the card the library holds, and the merged card is read for readiness.
    A field this patch carries that fails refuses the whole write. A field it leaves alone that fails
    is reported, and the write goes through. The workflow itself is never changed.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.
        body: The fields to change. An absent field keeps what the library holds.

    Returns:
        The refreshed card and its readiness reading.

    Raises:
        LibraryError: If the library does not hold the workflow.
        CardRefusedError: If a field the patch carries fails the readiness check.
        PresentationRefusedError: If the library refuses the author's words.
    """
    workflow = await session.library.fetch_workflow(workflow_id)
    held = await saved_card(session, workflow_id, workflow)
    title = body.title if body.title is not None else held.title
    summary = body.summary if body.summary is not None else held.summary
    applicability = body.applicability if body.applicability is not None else held.applicability
    patch = body.presentation
    words = effective_presentation(held.presentation, patch) if patch is not None and patch.changes_anything() else held.presentation
    readiness = card_readiness(workflow, prepare(workflow, session.registry), title=title, summary=summary, presentation=words)
    refused = [finding for finding in readiness.errors() if finding.field in _supplied(body)]
    if refused:
        message = refusal_text(readiness.model_copy(update={"findings": tuple(refused)}))
        raise CardRefusedError(message, readiness=readiness)
    await session.library.redescribe(
        workflow_id,
        title=title.strip(),
        summary=summary,
        applicability=applicability,
        # Absent leaves the library's own words alone, and a merge that changed nothing is absent.
        presentation=words if patch is not None and patch.changes_anything() else None,
    )
    return RedescribedCard(card=await saved_card(session, workflow_id, workflow), readiness=readiness)


def _supplied(body: WorkflowCardPatch) -> frozenset[ReadinessField]:
    """Which fields this patch actually carries, and therefore which of them may refuse it."""
    named: set[ReadinessField] = set()
    if body.title is not None:
        named.add("title")
    if body.summary is not None:
        named.add("summary")
    return frozenset(named) | (body.presentation.prose_fields() if body.presentation is not None else frozenset())
