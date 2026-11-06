"""The evidence log's reads by content address, without a file behind them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from core.ai.workflows.contracts import Approval, Grade, RunEvidence

    from harness.features.maintenance.internal_contracts import RunHistoryQueries


@dataclass
class FakeRunHistory:
    """Answers the events a test put in `events` whose content address is asked for, oldest first. Empty answers nothing."""

    events: list[RunEvidence | Approval | Grade] = field(default_factory=list)

    async def history_for(self, *, spec_hash: str) -> Sequence[RunEvidence | Approval | Grade]:
        """Every event filed under `spec_hash`."""
        return [event for event in self.events if event.spec_hash == spec_hash]

    async def history_for_any(self, *, spec_hashes: Collection[str]) -> Sequence[RunEvidence | Approval | Grade]:
        """Every event filed under any of `spec_hashes`."""
        return [event for event in self.events if event.spec_hash in spec_hashes]


_proof: type[RunHistoryQueries] = FakeRunHistory
"""Pyright fails this module the moment `FakeRunHistory` drifts from the `RunHistoryQueries` port."""
