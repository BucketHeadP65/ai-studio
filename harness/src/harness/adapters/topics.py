"""How a producer says a topic changed, and what carries it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.cqrs.contracts import Broadcaster


class BroadcastTopics:
    """Announces on the process's change channel."""

    def __init__(self, broadcaster: Broadcaster) -> None:
        """Announce through `broadcaster`.

        Args:
            broadcaster: The process's change channel.
        """
        self._broadcaster = broadcaster

    def publish(self, topic: str) -> None:
        """Announce `topic` on the change channel.

        Args:
            topic: The topic that changed.
        """
        self._broadcaster.publish(topic)


class NoTopics:
    """Announces nowhere, for a process with no change channel behind it."""

    def publish(self, topic: str) -> None:
        """Do nothing.

        Args:
            topic: The topic that changed, which is dropped.
        """
