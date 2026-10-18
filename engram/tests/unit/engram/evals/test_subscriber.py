"""Unit tests for the QA-generation indexing-event subscriber.

The subscriber's autogen branching is asserted with a recording fake queue; the default file-backed
`PendingPagesQueue` is exercised directly on `tmp_path` for its dedup + round-trip semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import PendingPagesQueue, QaGenerationSubscriber
from engram.indexing.contracts import CitingPagesChanged

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class FakeQaGenerationQueue:
    """A recording `QaGenerationQueue`: each enqueue call is appended for assertions."""

    def __init__(self) -> None:
        self.enqueued: list[list[str]] = []

    async def enqueue(self, page_ids: Sequence[str]) -> None:
        self.enqueued.append(list(page_ids))


class TestQaGenerationSubscriber:
    async def test_queue_mode_enqueues_paper_ids(self):
        queue = FakeQaGenerationQueue()
        subscriber = QaGenerationSubscriber(queue, autogen="queue")

        await subscriber(CitingPagesChanged(collection="research", page_ids=("paper.x", "paper.y")))

        assert queue.enqueued == [["paper.x", "paper.y"]]

    async def test_inline_mode_also_enqueues(self):
        queue = FakeQaGenerationQueue()
        subscriber = QaGenerationSubscriber(queue, autogen="inline")

        await subscriber(CitingPagesChanged(collection="research", page_ids=("paper.x",)))

        assert queue.enqueued == [["paper.x"]]

    async def test_off_mode_does_not_enqueue(self):
        queue = FakeQaGenerationQueue()
        subscriber = QaGenerationSubscriber(queue, autogen="off")

        await subscriber(CitingPagesChanged(collection="research", page_ids=("paper.x",)))

        assert queue.enqueued == []

    async def test_empty_paper_ids_does_not_enqueue(self):
        queue = FakeQaGenerationQueue()
        subscriber = QaGenerationSubscriber(queue, autogen="queue")

        await subscriber(CitingPagesChanged(collection="research", page_ids=()))

        assert queue.enqueued == []


class TestPendingPagesQueue:
    async def test_enqueue_then_pending_round_trips(self, tmp_path: Path):
        queue = PendingPagesQueue(tmp_path / "pending.json")

        await queue.enqueue(["paper.a", "paper.b"])

        assert queue.pending() == ["paper.a", "paper.b"]

    async def test_pending_is_empty_before_first_enqueue(self, tmp_path: Path):
        queue = PendingPagesQueue(tmp_path / "missing.json")

        assert queue.pending() == []

    async def test_enqueue_dedupes_preserving_first_seen_order(self, tmp_path: Path):
        queue = PendingPagesQueue(tmp_path / "pending.json")

        await queue.enqueue(["paper.a", "paper.b"])
        await queue.enqueue(["paper.b", "paper.c", "paper.a"])

        assert queue.pending() == ["paper.a", "paper.b", "paper.c"]
