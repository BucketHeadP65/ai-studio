"""Chooses the cut plans of a load's mirrors before their rows are built: stored plans first, else the chunk planner.

`CutPlanStage` finds each heading section a cut plan would cut, reads the plans stored for those sections under
the cut plan identity and shows the chunk planner only the sections with none. An accepted answer is stored,
so indexing the same text again with the same chunk planner calls no model. A planner call that fails, runs past
its deadline or answers numbers the domain refuses leaves its heading section with no plan, and the section is filled.
No planner call ever fails an index.
"""

from __future__ import annotations

import asyncio

from dataclasses import replace
from typing import TYPE_CHECKING

from core.observability import get_logger

from engram.indexing.domain.card import content_hash
from engram.indexing.domain.mirror_chunks import (
    DEFAULT_PLANNER_CEILING_TOKENS,
    accepted_starts,
    cut_plan_identity,
    heading_sections_for_planner,
    planner_paragraphs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import MirrorDoc
    from engram.indexing.internal_contracts import ChunkPlanner, CutPlanQueries, CutPlans

_logger = get_logger(__name__)

DEFAULT_PLANNER_DEADLINE_SECONDS = 60.0
"""How long one chunk planner call may take before its text is filled, when the host names no other deadline."""

DEFAULT_PLANNER_CONCURRENCY = 4
"""How many chunk planner calls may run at once, when the host names no other number."""


class CutPlanStage:
    """Gives each mirror of a load the cut plans of its heading sections, reusing stored plans and asking the chunk planner for the rest."""

    def __init__(  # noqa: PLR0913 (each limit is its own keyword, a wrapper object would just rename them)
        self,
        planner: ChunkPlanner,
        plans: CutPlans,
        queries: CutPlanQueries,
        *,
        ceiling_tokens: int = DEFAULT_PLANNER_CEILING_TOKENS,
        deadline_seconds: float = DEFAULT_PLANNER_DEADLINE_SECONDS,
        concurrency: int = DEFAULT_PLANNER_CONCURRENCY,
    ) -> None:
        """Create the stage over its chunk planner and the stored cut plans.

        Args:
            planner: Chooses where a heading section changes topic.
            plans: Stores each accepted cut plan.
            queries: Reads the cut plans already stored.
            ceiling_tokens: The most tokens of text the chunk planner is shown at once. Longer text is filled.
            deadline_seconds: How long one chunk planner call may take before its text is filled.
            concurrency: How many chunk planner calls may run at once.
        """
        self._planner = planner
        self._identity = cut_plan_identity(planner.identity)
        self._plans = plans
        self._queries = queries
        self._ceiling_tokens = ceiling_tokens
        self._deadline_seconds = deadline_seconds
        self._concurrency = concurrency

    @property
    def planner(self) -> str:
        """The cut plan identity: the chunk planner's identity and the paragraph numbering version, which plans are stored under."""
        return self._identity

    @property
    def ceiling_tokens(self) -> int:
        """The most tokens of text the chunk planner is shown at once."""
        return self._ceiling_tokens

    async def run(self, sources: Sequence[MirrorDoc], *, collection: str, sizes: ChunkSizes) -> tuple[MirrorDoc, ...]:
        """Return the mirrors, each carrying the cut plans of its heading sections: stored ones, else ones the chunk planner chose.

        Each distinct heading section text with no stored plan is shown to the chunk planner once. Each accepted answer is
        stored in one insert. A heading section whose call failed, ran late or was refused carries no plan and is not stored,
        so the next index asks again.

        Args:
            sources: The mirrors the load cuts into chunks.
            collection: The knowledge base the mirrors belong to, which the stored plans are keyed by.
            sizes: The chunk sizes the mirrors are cut by, which decide the heading sections a cut plan would cut.

        Returns:
            The mirrors in the order given, each with `cut_plans` by the content hash of each heading section that has one.
        """
        sections_by_source = [
            {content_hash(section): section for section in heading_sections_for_planner(source.text, sizes, ceiling_tokens=self._ceiling_tokens)}
            for source in sources
        ]
        sections = {text_hash: section for source_sections in sections_by_source for text_hash, section in source_sections.items()}
        if not sections:
            return tuple(sources)
        stored = await self._queries.cut_plans(collection, self._identity, sections.keys())
        asked = [text_hash for text_hash in sections if text_hash not in stored]
        limit = asyncio.Semaphore(self._concurrency)
        answers = await asyncio.gather(*(self._ask(sections[text_hash], limit) for text_hash in asked))
        chosen = {text_hash: starts for text_hash, starts in zip(asked, answers, strict=True) if starts is not None}
        if chosen:
            await self._plans.insert(collection, self._identity, chosen)
        _logger.info(
            "Cut plans for {collection}: {reused} reused, {chosen} chosen by {planner}, {filled} filled",
            collection=collection,
            reused=len(stored),
            chosen=len(chosen),
            planner=self._identity,
            filled=len(asked) - len(chosen),
        )
        plans = {**stored, **chosen}
        return tuple(
            replace(source, cut_plans={text_hash: plans[text_hash] for text_hash in source_sections if text_hash in plans})
            for source, source_sections in zip(sources, sections_by_source, strict=True)
        )

    async def _ask(self, section: str, limit: asyncio.Semaphore) -> tuple[int, ...] | None:
        """The chunk planner's accepted answer for one heading section, or `None` when the call failed, ran late or was refused."""
        paragraphs = planner_paragraphs(section)
        async with limit:
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    answer = await self._planner.chunk_starts(paragraphs)
            except Exception as exc:  # noqa: BLE001 (a failed or late planner call fills its text and never fails the index)
                _logger.warning(
                    "Chunk planner {planner} gave no cut plan for a heading section of {count} paragraphs, so it is filled: {reason}",
                    planner=self._identity,
                    count=len(paragraphs),
                    reason=repr(exc),
                )
                return None
        starts = accepted_starts(answer, len(paragraphs))
        if starts is None:
            _logger.warning(
                "Chunk planner {planner} answered {answer} for a heading section of {count} paragraphs, not strictly increasing paragraph numbers, so it is filled",
                planner=self._identity,
                answer=list(answer),
                count=len(paragraphs),
            )
        return starts
