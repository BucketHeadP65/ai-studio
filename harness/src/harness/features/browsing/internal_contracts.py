"""The browsing slice's own port: the library reads an import compares a workflow file with."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.ai.workflows.contracts import SavedWorkflow, Workflow


class SavedVersionQueries(Protocol):
    """The library reads that compare a workflow file with the versions the library holds of its workflow."""

    async def locate(self, name: str, version: int) -> str | None:
        """The id of the page holding `name` at `version`, or None when there is no such page.

        Args:
            name: The workflow's name.
            version: The version to find.

        Returns:
            The page id, or None.
        """
        ...

    async def current(self, workflow_id: str) -> str:
        """The newest live version of the workflow `workflow_id` names, or `workflow_id` itself when no newer one is live.

        Args:
            workflow_id: A workflow version's id.

        Returns:
            The id of the newest live version, or `workflow_id`.
        """
        ...

    async def versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """Every saved version of one workflow, live, superseded or archived, lowest version first.

        Args:
            name: The workflow's name.

        Returns:
            Each version's `(version, id)`. Empty when no version of `name` is saved.
        """
        ...

    async def saved_by(self, workflow_id: str) -> str:
        """Who saved one workflow version, as its page records it.

        Args:
            workflow_id: The workflow version's id. Superseded and archived versions are answered too.

        Returns:
            The `saved_by` its save recorded.

        Raises:
            WorkflowUnknownError: No workflow page has that id.
            LibraryError: The page records no `saved_by`.
        """
        ...

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The workflow one version holds.

        Args:
            workflow_id: A workflow version's id.

        Returns:
            The validated workflow.

        Raises:
            WorkflowUnknownError: No workflow has that id.
        """
        ...

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """The card one version is saved under: its title, summary, applicability and presentation.

        Args:
            workflow_id: A workflow version's id.

        Returns:
            The card, or None when no page has that id.
        """
        ...
