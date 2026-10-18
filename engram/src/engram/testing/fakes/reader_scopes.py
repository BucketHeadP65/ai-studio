"""An in-memory `ReaderScopes` for unit tests.

`FakeReaderScopes` records every `scope` call as `(collection, private)` in `calls`, in order, and touches no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeReaderScopes:
    """Records every scope call and gives no role to anything."""

    calls: list[tuple[str, bool]] = field(default_factory=list)

    async def scope(self, collection: str, *, private: bool) -> None:
        """Record `(collection, private)`.

        Args:
            collection: The knowledge base's collection.
            private: Whether the knowledge base declares itself private.
        """
        self.calls.append((collection, private))
