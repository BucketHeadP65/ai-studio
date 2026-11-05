"""The ports the maintenance slice's watches read through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.workflows.contracts import Approval, Grade, RunEvidence


class RunHistoryQueries(Protocol):
    """The evidence log's read by content address: every run, approval and grade one workflow version holds."""

    async def history_for(self, *, spec_hash: str) -> Sequence[RunEvidence | Approval | Grade]:
        """Everything the evidence log holds about one workflow version.

        Args:
            spec_hash: The version's content address. It must not be empty.

        Returns:
            Its events, oldest first.

        Raises:
            ValueError: If `spec_hash` is empty.
        """
        ...
