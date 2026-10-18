"""A change channel that keeps what it was told instead of opening two database connections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.cqrs.events import ChangeNotification

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from core.cqrs.contracts import TopicSink


class FakeBroadcaster:
    """Records every `publish`, `start` and `stop`, and replays what it was delivered to a subscriber."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.delivered: list[str] = []
        self.started = 0
        self.stopped = 0
        self._subscriber_count = 0

    async def start(self) -> None:
        """Count the start. Nothing is opened."""
        self.started += 1

    async def stop(self) -> None:
        """Count the stop. Nothing is closed."""
        self.stopped += 1

    def publish(self, topic: str) -> None:
        """Keep `topic` instead of announcing it."""
        self.published.append(topic)

    def deliver(self, topic: str) -> None:
        """Keep `topic` as though the listening connection had heard it."""
        self.delivered.append(topic)

    @property
    def pending(self) -> list[str]:
        """Always empty: this fake keeps a `publish` at once, it never queues one."""
        return []

    @property
    def subscriber_count(self) -> int:
        """How many `subscribe()` generators are currently open."""
        return self._subscriber_count

    def subscribe(self) -> AsyncGenerator[ChangeNotification]:
        """Everything delivered so far, once, in order."""
        return self._replay()

    async def _replay(self) -> AsyncGenerator[ChangeNotification]:
        self._subscriber_count += 1
        try:
            for topic in self.delivered:
                yield ChangeNotification(topic=topic, occurred_at=datetime.now(UTC))
        finally:
            self._subscriber_count -= 1


_proof: type[TopicSink] = FakeBroadcaster
"""Pyright fails this module the moment `FakeBroadcaster` drifts from the `TopicSink` port."""
