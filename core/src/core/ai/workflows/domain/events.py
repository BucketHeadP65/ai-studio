"""The events the workflow library raises, written to the outbox in the transaction that writes their page."""

from __future__ import annotations

from core.cqrs.contracts import Event


class ReusableStepSaved(Event):
    """A reusable step version above every saved version of its name was written to the library.

    It is raised once per new version, in the transaction that writes the version's page. Saving a version again, a
    version below the newest, or new cases under an unchanged version raises nothing.
    """

    step: str
    """The reusable step's name."""

    version: int
    """The version just saved."""

    step_id: str
    """The saved version's page id, `st.<name>.v<version>`."""

    step_hash: str
    """The saved step's content address, `step_content_address` of the step."""

    saved_by: str
    """Who saved it, as the save's provenance names them."""

    superseded: tuple[str, ...] = ()
    """The page ids of the live lower versions this save links forward to it, lowest version first."""
