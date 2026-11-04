"""The nodes a run has work in flight on, for an observer that shows what the run is doing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NodesInFlight:
    """How many started reports of each node have not finished yet.

    A map reports `started` and `finished` once per item under its own node id, so a node is
    running until every report it started has finished.
    """

    _counts: dict[str, int] = field(default_factory=dict)

    @property
    def running(self) -> list[str]:
        """The nodes with work in flight, in the order each first started."""
        return list(self._counts)

    def start(self, node_id: str) -> None:
        """Count one more report of `node_id` in flight.

        Args:
            node_id: The node that started.
        """
        self._counts[node_id] = self._counts.get(node_id, 0) + 1

    def finish(self, node_id: str) -> bool:
        """Count one report of `node_id` finished.

        Args:
            node_id: The node that finished.

        Returns:
            Whether the node has nothing left in flight, which is also true for a node never started.
        """
        left = self._counts.get(node_id, 0) - 1
        if left > 0:
            self._counts[node_id] = left
            return False
        self._counts.pop(node_id, None)
        return True

    def clear(self) -> None:
        """Forget every node."""
        self._counts.clear()
