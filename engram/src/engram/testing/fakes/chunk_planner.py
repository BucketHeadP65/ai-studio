"""Fake chunk planner and stored cut plans: scripted answers and in-memory plans, with every call recorded as public state."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence


class FakeChunkPlanner:
    """A `ChunkPlanner` that answers the same paragraph numbers for every text and records the paragraphs of each call.

    It raises `error` when one is given, waits for ever when `never_answers` is set, and pauses `pause_seconds`
    before each answer. `most_at_once` holds the most calls that were in progress at the same time.
    """

    def __init__(
        self,
        starts: Sequence[int] = (),
        *,
        identity: str = "fake-planner/prompt-1",
        error: Exception | None = None,
        never_answers: bool = False,
        pause_seconds: float = 0.0,
    ) -> None:
        """Script the planner's answer.

        Args:
            starts: The paragraph numbers every call answers.
            identity: The model name, prompt version and thinking level the planner reports.
            error: The error every call raises, or `None` to answer.
            never_answers: Whether every call waits for ever instead of answering.
            pause_seconds: How long each call waits before it answers.
        """
        self.starts = list(starts)
        self.identity = identity
        self.error = error
        self.never_answers = never_answers
        self.pause_seconds = pause_seconds
        self.calls: list[list[str]] = []
        self.most_at_once = 0
        self._in_progress = 0

    async def chunk_starts(self, paragraphs: Sequence[str]) -> Sequence[int]:
        """Record `paragraphs`, then answer the scripted numbers, raise the scripted error or never answer.

        Args:
            paragraphs: The text's paragraphs in reading order.

        Returns:
            The scripted paragraph numbers.

        Raises:
            Exception: The scripted `error`, when one is given.
        """
        self.calls.append(list(paragraphs))
        self._in_progress += 1
        self.most_at_once = max(self.most_at_once, self._in_progress)
        try:
            if self.error is not None:
                raise self.error
            if self.never_answers:
                await asyncio.Event().wait()
            await asyncio.sleep(self.pause_seconds)
            return self.starts
        finally:
            self._in_progress -= 1


class FakeCutPlans:
    """In-memory stored cut plans serving both the write and the read port, keyed by knowledge base, planner and text hash."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str, str], tuple[int, ...]] = {}
        self.inserts: list[tuple[str, str, dict[str, tuple[int, ...]]]] = []

    async def insert(self, collection: str, planner: str, plans: Mapping[str, tuple[int, ...]]) -> None:
        """Store each plan, keeping a plan already stored under the same key, and record the insert.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            plans: The paragraph numbers by text hash.
        """
        self.inserts.append((collection, planner, dict(plans)))
        for text_hash, starts in plans.items():
            self.stored.setdefault((collection, planner, text_hash), starts)

    async def cut_plans(self, collection: str, planner: str, text_hashes: Collection[str]) -> dict[str, tuple[int, ...]]:
        """The stored plans for the asked text hashes, by text hash.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            text_hashes: The text hashes asked for.

        Returns:
            The paragraph numbers by text hash, without the hashes no plan is stored for.
        """
        return {text_hash: self.stored[key] for text_hash in text_hashes if (key := (collection, planner, text_hash)) in self.stored}
