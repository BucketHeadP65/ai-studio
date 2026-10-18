"""The JSON file queue of page ids waiting for evaluation dataset generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# Validates the queue file's JSON as a list of ids.
_IDS_ADAPTER = TypeAdapter(list[str])


class PendingPagesQueue:
    """A `QaGenerationQueue` kept in a JSON file: page ids in first-seen order, each held once.

    The file is created on the first `enqueue`.
    """

    def __init__(self, path: Path) -> None:
        """Hold the queue file's path.

        Args:
            path: The JSON queue file. It need not exist yet.
        """
        self._path = path

    async def enqueue(self, page_ids: Sequence[str]) -> None:
        """Add page ids to the queue file, keeping first-seen order and dropping duplicates.

        Args:
            page_ids: The page ids to add.
        """
        merged = list(dict.fromkeys([*self.pending(), *page_ids]))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(_IDS_ADAPTER.dump_json(merged, indent=2))

    def pending(self) -> list[str]:
        """The page ids waiting in the queue, in first-seen order.

        Returns:
            The ids, empty when the queue file does not exist yet.
        """
        if not self._path.exists():
            return []
        return _IDS_ADAPTER.validate_json(self._path.read_bytes())
