"""The ports the source queue connector and its resolvers depend on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from engram.sources.contracts import Resolved, SourceQueueEntry


class Resolver(Protocol):
    """One source type's way from a source queue row to rendered markdown."""

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Resolve one row into rendered markdown and the facts its mirror carries.

        Args:
            entry: The source queue row to resolve.

        Returns:
            The rendered markdown with its title, URL and converter label.

        Raises:
            ResolveError: The row cannot be resolved. The message is the reason the queue records.
        """
        ...
