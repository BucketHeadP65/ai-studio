"""A run-end observer that yields to the event loop before it returns, as the scheduler's database read does."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence


@dataclass
class FakeYieldingRunEndObserver:
    """Records each run ending it is told about, after yielding to the event loop `yields` times."""

    yields: int = 3
    endings: list[RunEvidence] = field(default_factory=list)

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Yield `yields` times, then record `evidence`."""
        for _ in range(self.yields):
            await asyncio.sleep(0)
        self.endings.append(evidence)
