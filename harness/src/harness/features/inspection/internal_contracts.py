"""The ports of the inspection slice: the record's agent runs, and what answers a follow-up to the inspector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.ai.history.contracts import RunRow
    from core.ai.inspection.contracts import StoredQuestion

    from harness.features.inspection.domain import Answered


class RunQueries(Protocol):
    """The record's agent runs, read by id."""

    async def run(self, run_id: str) -> RunRow | None:
        """One agent run's row in the record.

        Args:
            run_id: The agent run's id.

        Returns:
            The agent run's row, or `None` when the record holds no agent run with that id.
        """
        ...


class FollowUpAnswerer(Protocol):
    """Continues one question's conversation with the inspector, one more answer at a time."""

    async def answer(self, asked: StoredQuestion, text: str) -> Answered:
        """Continue one question's conversation on its own tier, with the same tools over the same run.

        The exchange is recorded under the same conversation, so a later follow-up sees it too.
        When the question's run cannot be opened, the loader's `UNREADABLE` errors pass through unchanged.

        Args:
            asked: The question whose conversation continues.
            text: What to ask, in words.

        Returns:
            The inspector's answer, the conversation it is recorded under, and the model that gave it.

        Raises:
            RunEmptyError: when the question's run recorded no step.
            LookupError: when the question's source holds no such run.
            FileNotFoundError: when the file the question's source names does not exist.
            ValueError: when what the question's source names does not read as a transcript.
        """
        ...
