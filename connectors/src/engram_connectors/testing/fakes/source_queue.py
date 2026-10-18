"""In-memory `Resolver` fake for `SourceQueueConnector` tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram_connectors.source_queue.contracts import ResolveError

if TYPE_CHECKING:
    from engram.sources.contracts import Resolved, SourceQueueEntry


@dataclass
class FakeResolver:
    """Resolver that records every call and answers by the key's prefix.

    The prefix is the part of the entry key before the first hyphen (`yt`, `arxiv`, `x`, `url`).
    It raises the error seeded for the prefix, or returns the result seeded for it.
    """

    results: dict[str, Resolved] = field(default_factory=dict)
    """The result returned for each key prefix."""
    errors: dict[str, str] = field(default_factory=dict)
    """The `ResolveError` message raised for each key prefix. It is checked before `results`."""
    calls: list[str] = field(default_factory=list)
    """Every entry key asked for, in order."""

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Record the entry key, then answer from the seeded errors and results for its prefix.

        Args:
            entry: The source queue entry to resolve.

        Returns:
            The result seeded for the key's prefix.

        Raises:
            ResolveError: With the message seeded in `errors` for the prefix.
            KeyError: When the prefix has no seeded result.
        """
        self.calls.append(entry.key)
        prefix = entry.key.split("-", 1)[0]
        if prefix in self.errors:
            raise ResolveError(self.errors[prefix])
        return self.results[prefix]
