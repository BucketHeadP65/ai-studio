"""Chooses the cut plans of a load's mirrors before their rows are built: stored plans first, else the chunk planner.

`CutPlanStage` finds each run of mirror text a cut plan would cut, reads the plans stored for those runs under
the chunk planner's identity and shows the chunk planner only the runs with none. An accepted answer is stored,
so indexing the same text again with the same chunk planner calls no model. A planner call that fails, runs past
its deadline or answers numbers the domain refuses leaves its run with no plan, and the run is filled. No planner
call ever fails an index.
"""

from __future__ import annotations

import asyncio

from dataclasses import replace
from typing import TYPE_CHECKING

from core.observability import get_logger

from engram.indexing.domain.card import content_hash
from engram.indexing.domain.mirror_chunks import DEFAULT_PLANNER_CEILING_TOKENS, accepted_starts, planner_paragraphs, runs_for_planner

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
    """Gives each mirror of a load the cut plans of its runs, reusing stored plans and asking the chunk planner for the rest."""

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
            planner: Chooses where a run of text changes topic.
            plans: Stores each accepted cut plan.
            queries: Reads the cut plans already stored.
            ceiling_tokens: The most tokens of text the chunk planner is shown at once. Longer text is filled.
            deadline_seconds: How long one chunk planner call may take before its text is filled.
            concurrency: How many chunk planner calls may run at once.
        """
        self._planner = planner
        self._plans = plans
        self._queries = queries
        self._ceiling_tokens = ceiling_tokens
        self._deadline_seconds = deadline_seconds
        self._concurrency = concurrency

    @property
    def planner(self) -> str:
        """The chunk planner's identity: its model name and prompt version."""
        return self._planner.identity

    @property
    def ceiling_tokens(self) -> int:
        """The most tokens of text the chunk planner is shown at once."""
        return self._ceiling_tokens

    async def run(self, sources: Sequence[MirrorDoc], *, collection: str, sizes: ChunkSizes) -> tuple[MirrorDoc, ...]:
        """Return the mirrors, each carrying the cut plans of its runs: stored ones, else ones the chunk planner chose.

        Each distinct run with no stored plan is shown to the chunk planner once. Each accepted answer is stored in one
        insert. A run whose call failed, ran late or was refused carries no plan and is not stored, so the next index asks
        again.

        Args:
            sources: The mirrors the load cuts into chunks.
            collection: The knowledge base the mirrors belong to, which the stored plans are keyed by.
            sizes: The chunk sizes the mirrors are cut by, which decide the runs a cut plan would cut.

        Returns:
            The mirrors in the order given, each with `cut_plans` by the content hash of each run that has one.
        """
        runs_by_source = [
            {content_hash(run): run for run in runs_for_planner(source.text, sizes, ceiling_tokens=self._ceiling_tokens)} for source in sources
        ]
        runs = {text_hash: run for source_runs in runs_by_source for text_hash, run in source_runs.items()}
        if not runs:
            return tuple(sources)
        stored = await self._queries.cut_plans(collection, self._planner.identity, runs.keys())
        asked = [text_hash for text_hash in runs if text_hash not in stored]
        limit = asyncio.Semaphore(self._concurrency)
        answers = await asyncio.gather(*(self._ask(runs[text_hash], limit) for text_hash in asked))
        chosen = {text_hash: starts for text_hash, starts in zip(asked, answers, strict=True) if starts is not None}
        if chosen:
            await self._plans.insert(collection, self._planner.identity, chosen)
        _logger.info(
            "Cut plans for {collection}: {reused} reused, {chosen} chosen by {planner}, {filled} filled",
            collection=collection,
            reused=len(stored),
            chosen=len(chosen),
            planner=self._planner.identity,
            filled=len(asked) - len(chosen),
        )
        plans = {**stored, **chosen}
        return tuple(
            replace(source, cut_plans={text_hash: plans[text_hash] for text_hash in source_runs if text_hash in plans})
            for source, source_runs in zip(sources, runs_by_source, strict=True)
        )

    async def _ask(self, run: str, limit: asyncio.Semaphore) -> tuple[int, ...] | None:
        """The chunk planner's accepted answer for one run, or `None` when the call failed, ran late or was refused."""
        paragraphs = planner_paragraphs(run)
        async with limit:
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    answer = await self._planner.chunk_starts(paragraphs)
            except Exception as exc:  # noqa: BLE001 (a failed or late planner call fills its text and never fails the index)
                _logger.warning(
                    "Chunk planner {planner} gave no cut plan for a run of {count} paragraphs, so it is filled: {reason}",
                    planner=self._planner.identity,
                    count=len(paragraphs),
                    reason=repr(exc),
                )
                return None
        starts = accepted_starts(answer, len(paragraphs))
        if starts is None:
            _logger.warning(
                "Chunk planner {planner} answered {answer} for a run of {count} paragraphs, not strictly increasing paragraph numbers, so it is filled",
                planner=self._planner.identity,
                answer=list(answer),
                count=len(paragraphs),
            )
        return starts
