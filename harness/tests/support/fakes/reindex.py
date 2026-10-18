"""Reindex doubles for run indexing: one that refuses a knowledge base, one that watches the run's ending."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import RunFinished
from engram.authoring.contracts import ReindexResult

from harness.features.running.infrastructure import run_events

if TYPE_CHECKING:
    from engram.authoring.contracts import AuthoringKnowledgeBase

    from harness.session import Runtime


@dataclass
class FakeRefusingReindex:
    """Raises for the knowledge base named `refused` and records every other one it indexes."""

    refused: str
    indexed: list[str] = field(default_factory=list)

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Raise for `refused`, else record the knowledge base and answer clean."""
        if knowledge_base.name == self.refused:
            msg = f"no studio for {knowledge_base.name}"
            raise OSError(msg)
        self.indexed.append(knowledge_base.name)
        return ReindexResult(outcome="clean", detail=f"reindexed {knowledge_base.path}")


@dataclass
class FakeEndingAwareReindex:
    """Records each knowledge base it indexes, and whether `session`'s run had recorded its ending at that moment.

    With `release` given, each call sets `entered` and then waits for `release`, so a test can act while an
    index is under way.
    """

    session: Runtime | None = None
    release: asyncio.Event | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    indexed: list[str] = field(default_factory=list)
    ended_before_index: list[bool] = field(default_factory=list)

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Record the knowledge base and what the run had recorded, wait for `release` when given, and answer clean."""
        self.indexed.append(knowledge_base.name)
        if self.session is not None:
            recorded = run_events.events_for(self.session.state, self.session.active.run_id or "")
            self.ended_before_index.append(any(isinstance(entry.event, RunFinished) for entry in recorded))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return ReindexResult(outcome="clean", detail=f"reindexed {knowledge_base.path}")
